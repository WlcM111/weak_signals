#!/usr/bin/env python3
"""Полная аналитика по шести темам датасета: прогон заданий, метрики и все данные в один txt-файл.

Для каждой темы скрипт ставит задание через API orchestrator, ждёт терминального статуса,
забирает выдачу, полные инсайты, исключённых кандидатов, запуски адаптеров и статистику анализа
из PostgreSQL и сверяет выдачу со строками датасета по той же теме. Секреты в отчёт не попадают:
ключ API читается из `.env` и используется только в заголовке запросов.

Запуск из корня репозитория: `python3 tools/theme_analytics.py`. Только стандартная библиотека.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API = os.environ.get("WS_ANALYTICS_API", "http://127.0.0.1:8080")
TOP_N = int(os.environ.get("WS_ANALYTICS_TOP_N", "15"))
POLL_SECONDS = float(os.environ.get("WS_ANALYTICS_POLL_SECONDS", "10"))
PAUSE_BETWEEN_THEMES = float(os.environ.get("WS_ANALYTICS_PAUSE_SECONDS", "30"))
JOB_TIMEOUT_SECONDS = float(os.environ.get("WS_ANALYTICS_JOB_TIMEOUT_SECONDS", "1500"))
# Стенд после перезапуска analyzer прогревает модель эмбеддингов: пока он не готов, задания падают.
READY_TIMEOUT_SECONDS = float(os.environ.get("WS_ANALYTICS_READY_TIMEOUT_SECONDS", "600"))
READY_POLL_SECONDS = float(os.environ.get("WS_ANALYTICS_READY_POLL_SECONDS", "5"))
DATASET = Path("ml/data/raw/dataset_normalized.csv")
TERMINAL = {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}
# Тема датасета (колонка domain) → запрос в свободной форме, как его вводит пользователь.
THEMES = [
    ("Индустриальный ИИ", "индустриальный искусственный интеллект на производстве"),
    ("Роботы", "робототехника и физический искусственный интеллект"),
    ("Инфраструктура ИИ", "инфраструктура для искусственного интеллекта и дата-центры"),
    ("Финтех", "перспективные решения в финтехе"),
    ("Защита ИИ", "защита и безопасность систем искусственного интеллекта"),
    ("Edge", "edge-вычисления и периферийный инференс"),
]
# Общие слова, которые есть почти в любом названии и дали бы ложные совпадения.
STOPWORDS = {
    "для", "при", "как", "или", "без", "под", "над", "это", "что", "the", "and", "for", "with", "from",
    "искусственный", "искусственного", "интеллект", "интеллекта", "технология", "технологии", "система",
    "системы", "систем", "платформа", "платформы", "решение", "решения", "технология:",
}
# Основы (первые 5 букв) слов, общих для целых тем: совпадение только по ним ничего не значит.
# Список составлен по ложным совпадениям прогона 2026-09-22: «безопасность», «защита», «edge»,
# «вычисления», «контроль», «архитектура» совпадали у заведомо разных технологий.
GENERIC_STEMS = {
    "безоп", "защит", "edge", "вычис", "контр", "архит", "управл", "разра", "анали", "обесп",
    "испол", "основ", "модел", "данны", "техно", "интел", "искус", "систе", "платф", "решен",
    "инфра", "прило", "иссле", "разви", "произ", "промы", "цифро", "servi", "secur", "compu",
}
MATCH_MIN_SHARED = 2
MATCH_MIN_OVERLAP = 0.4
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")


def api_key() -> str:
    """Ключ API из `.env` (последнее незакомментированное определение); пустая строка — без ключа."""
    value = ""
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("WS_API_KEY="):
            value = line.split("=", 1)[1].strip()
    return value


def request(method: str, path: str, body: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None) -> tuple[int, Any]:
    """HTTP-запрос к API: (код, JSON или текст). Сетевые ошибки возвращаются кодом 0."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    if KEY:
        req.add_header("X-API-Key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as err:
        raw, status = err.read().decode("utf-8", "replace"), err.code
    except (urllib.error.URLError, TimeoutError) as err:
        return 0, str(err)
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def run(*args: str) -> str:
    """Команда docker compose; при ошибке — текст ошибки вместо вывода."""
    try:
        result = subprocess.run(["docker", "compose", *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as err:
        return f"ошибка: {err}"
    return result.stdout.strip() if result.returncode == 0 else f"ошибка: {result.stderr.strip()[:300]}"


def sql(query: str) -> list[list[str]]:
    """Строки результата SQL в базе weaksignals (поля через табуляцию)."""
    output = run("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "weaksignals",
                 "-At", "-F", "\t", "-c", query)
    if output.startswith("ошибка:"):
        return [[output]]
    return [line.split("\t") for line in output.splitlines() if line]


def env_of(service: str, name: str) -> str:
    """Несекретная переменная окружения контейнера; пустая строка — не задана."""
    value = run("exec", "-T", service, "printenv", name)
    return "" if value.startswith("ошибка:") else value


def stems(text: str) -> set[str]:
    """Основы слов (первые 5 букв) без общих слов — для грубой сверки названий."""
    words = re.findall(r"[a-zа-яё0-9]+", text.lower())
    return {word[:5] for word in words if len(word) >= 3 and word not in STOPWORDS} - GENERIC_STEMS


def best_match(title: str, rows: list[dict[str, str]]) -> tuple[dict[str, str], set[str], float] | None:
    """Наиболее похожая строка датасета по пересечению основ; None, если совпадение слабое."""
    title_stems = stems(title)
    best: tuple[dict[str, str], set[str], float] | None = None
    for row in rows:
        row_stems = stems(row["tech"])
        shared = title_stems & row_stems
        if not title_stems or not row_stems:
            continue
        overlap = len(shared) / min(len(title_stems), len(row_stems))
        if len(shared) >= MATCH_MIN_SHARED and overlap >= MATCH_MIN_OVERLAP and (best is None or overlap > best[2]):
            best = (row, shared, overlap)
    return best


def pct(value: Any) -> str:
    """Доля в процентах."""
    return f"{float(value) * 100:.1f}%" if isinstance(value, (int, float)) else "—"


class Report:
    """Текстовый отчёт: пишется построчно и сразу сбрасывается на диск."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = path.open("w", encoding="utf-8")

    def line(self, text: str = "") -> None:
        self.handle.write(text + "\n")
        self.handle.flush()

    def title(self, text: str) -> None:
        self.line()
        self.line("═" * 100)
        self.line(text)
        self.line("═" * 100)


def wait_until_ready(report: Report) -> bool:
    """Ждёт готовности API и активной модели; False — стенд не готов за отведённое время."""
    started = time.monotonic()
    last = ""
    while time.monotonic() - started < READY_TIMEOUT_SECONDS:
        ready_status, _ = request("GET", "/readyz")
        model_status, model = request("GET", "/api/v1/model")
        if ready_status == 200 and model_status == 200 and isinstance(model, dict) and model.get("model_version_id"):
            waited = time.monotonic() - started
            report.line(f"стенд готов через {waited:.0f} с ожидания: модель {model['model_version_id']}")
            print(f"стенд готов ({waited:.0f} с)", flush=True)
            return True
        state = f"/readyz {ready_status}, /api/v1/model {model_status}"
        if state != last:
            report.line(f"  ожидание готовности: {state}")
            print(f"  ожидание готовности: {state}", flush=True)
            last = state
        time.sleep(READY_POLL_SECONDS)
    report.line(f"стенд не готов за {READY_TIMEOUT_SECONDS:.0f} с — прогон прерван")
    print("стенд не готов: прогон прерван", flush=True)
    return False


def preflight(report: Report) -> bool:
    """Состояние стенда, модель и несекретная конфигурация до прогона; False — стенд не готов."""
    report.title("СОСТОЯНИЕ СТЕНДА ПЕРЕД ПРОГОНОМ")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    report.line(f"коммит: {commit.stdout.strip() or 'нет данных git'}")
    status, _ = request("GET", "/readyz")
    report.line(f"готовность API (/readyz): код {status}")
    report.line("контейнеры:")
    for row in run("ps", "--format", "{{.Service}}\t{{.State}}\t{{.Status}}").splitlines():
        report.line("  " + " | ".join(row.split("\t")))
    if not wait_until_ready(report):
        return False
    status, model = request("GET", "/api/v1/model")
    report.line(f"модель (/api/v1/model): код {status}")
    if isinstance(model, dict):
        for field in ("model_version_id", "model_family", "embedding_model", "dataset_version",
                      "feature_schema_version", "trained_at"):
            report.line(f"  {field}: {model.get(field)}")
        report.line(f"  метрики: {json.dumps(model.get('metrics'), ensure_ascii=False)}")
        report.line(f"  признаков: {len(model.get('feature_names') or [])}")
    report.line("пороги и провайдеры (несекретные переменные):")
    for service, names in (
        ("analyzer", ("WS_CLUSTER_DISTANCE_THRESHOLD", "WS_MIN_DOC_QUERY_SIM", "WS_MIN_CANDIDATE_QUERY_SIM",
                      "WS_TRUST_WEIGHTED_SELECTION", "WS_REQUIRE_HIGH_TRUST_SOURCE")),
        ("insight", ("WS_LLM_PRIMARY_PROVIDER", "WS_LLM_FALLBACK_PROVIDER", "WS_GIGACHAT_MODEL",
                     "WS_GIGACHAT_MAX_CONCURRENCY")),
    ):
        for name in names:
            report.line(f"  {service}.{name} = {env_of(service, name) or '(не задано, значение по умолчанию)'}")
    report.line("каталог источников (collector.source_catalog):")
    for row in sql("SELECT source_key, enabled, rate_limit_rps, default_source_type, default_trust "
                   "FROM collector.source_catalog ORDER BY source_key"):
        report.line("  " + " | ".join(row))
    return True


def wait_for(job_id: str, report: Report) -> dict[str, Any]:
    """Опрос задания до терминального статуса; при превышении времени — отмена."""
    started = time.monotonic()
    last_stage = ""
    job: dict[str, Any] = {}
    while True:
        status, body = request("GET", f"/api/v1/jobs/{job_id}")
        if status == 200 and isinstance(body, dict):
            job = body
            stage = job.get("status", "")
            if stage != last_stage:
                report.line(f"  {datetime.now(UTC).strftime('%H:%M:%S')} UTC  статус: {stage}")
                print(f"    {stage}", flush=True)
                last_stage = stage
            if stage in TERMINAL:
                return job
        if time.monotonic() - started > JOB_TIMEOUT_SECONDS:
            request("POST", f"/api/v1/jobs/{job_id}/cancel")
            report.line(f"  превышено время ожидания {JOB_TIMEOUT_SECONDS:.0f} с: задание отменено")
            return job
        time.sleep(POLL_SECONDS)


def write_item(report: Report, item: dict[str, Any], dataset_rows: list[dict[str, str]]) -> bool:
    """Полная карточка инсайта; возвращает, нашлось ли эвристическое совпадение с датасетом."""
    report.line()
    report.line(f"  #{item.get('rank')} [{item.get('narrative_status')} | {item.get('confidence_band')} | "
                f"скоринг {pct(item.get('score'))}] {item.get('title_ru')}")
    report.line(f"     автоназвание кластера: {item.get('title_auto')}")
    report.line(f"     решение модели: {item.get('decision_explanation_ru')}")
    features = {f.get("feature_name"): f for f in item.get("features") or []}
    similarity = features.get("emb_sim_query", {}).get("value")
    report.line(f"     близость к запросу (emb_sim_query): "
                f"{similarity:.3f}" if isinstance(similarity, (int, float)) else "     близость к запросу: —")
    report.line("     ключевые предикторы:")
    for feature in item.get("key_predictors") or []:
        report.line(f"       {feature.get('label_ru')}: значение {feature.get('value'):.3f}, "
                    f"вклад {feature.get('contribution'):+.2f} ({feature.get('direction')})")
    report.line(f"     стадия / тренд (прогноз): {item.get('predicted_stage')} / {item.get('predicted_trend')}")
    report.line(f"     описание: {item.get('description_ru')}")
    report.line(f"     преимущество: {item.get('advantage_ru')}")
    report.line(f"     кейс-пример: {item.get('case_example_ru')} (документ {item.get('case_document_id')})")
    report.line(f"     объяснение статуса: {item.get('explanation_ru')}")
    provenance = item.get("provenance") or {}
    report.line(f"     происхождение: провайдер {provenance.get('llm_provider')}, "
                f"модель {provenance.get('llm_model')}, "
                f"промпт {provenance.get('prompt_version')}, классификатор {provenance.get('model_version_id')}")
    sources = item.get("sources") or []
    report.line(f"     источники ({len(sources)}):")
    for source in sources:
        report.line(f"       - [{source.get('source_key')} | {source.get('source_type')} | "
                    f"{source.get('trust_level')} | "
                    f"{source.get('language_code')} | {(source.get('published_at') or '—')[:10]} | "
                    f"{source.get('summary_kind')}] {source.get('title')}")
        report.line(f"         {source.get('url')}")
        report.line(f"         резюме: {source.get('summary_ru')}")
    match = best_match(item.get("title_ru") or "", dataset_rows)
    if match:
        row, shared, overlap = match
        report.line(f"     возможное совпадение с датасетом (эвристика, проверить вручную): "
                    f"#{row['n']} «{row['tech']}», общие основы {sorted(shared)}, пересечение {overlap:.2f}")
    else:
        report.line("     совпадений с датасетом по названию не найдено (эвристика)")
    return match is not None


def analyze_theme(report: Report, index: int, domain: str, query: str, run_id: str,
                  dataset: list[dict[str, str]]) -> dict[str, Any]:
    """Прогон одной темы и полный раздел отчёта; возвращает строку сводки."""
    summary: dict[str, Any] = {"domain": domain, "query": query}
    report.title(f"ТЕМА {index}/6: {domain} — запрос «{query}»")
    status, accepted = request("POST", "/api/v1/queries", {"query_text": query, "top_n": TOP_N},
                               {"Idempotency-Key": f"analytics-{run_id}-{index}"})
    if status not in (200, 201, 202) or not isinstance(accepted, dict) or "job_id" not in accepted:
        report.line(f"задание не поставлено: код {status}, ответ {accepted}")
        summary["status"] = f"не поставлено ({status})"
        return summary
    job_id = accepted["job_id"]
    report.line(f"job_id: {job_id}   top_n: {TOP_N}")
    job = wait_for(job_id, report)
    summary["status"] = job.get("status", "нет данных")
    report.line(f"итоговый статус: {job.get('status')}")
    report.line(f"ошибка задания: {json.dumps(job.get('error'), ensure_ascii=False)}")
    report.line(f"создано / начато / завершено: {job.get('created_at')} / {job.get('started_at')} / "
                f"{job.get('finished_at')}")
    try:
        started = datetime.fromisoformat(str(job.get("started_at")).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(job.get("finished_at")).replace("Z", "+00:00"))
        summary["duration"] = f"{(finished - started).total_seconds():.0f} с"
    except ValueError:
        summary["duration"] = "—"
    report.line(f"длительность: {summary['duration']}")
    report.line(f"прогресс: {json.dumps(job.get('progress'), ensure_ascii=False)}")

    links = sql(f"SELECT collection_id, analysis_id FROM orchestrator.jobs WHERE job_id = '{job_id}'") \
        if UUID_RE.match(job_id) else []
    link = (links[0] + ["", ""])[:2] if links else ["", ""]
    collection_id = link[0] if UUID_RE.match(link[0]) else ""
    analysis_id = link[1] if UUID_RE.match(link[1]) else ""
    if not collection_id:
        report.line(f"связь задания с коллекцией не получена: {links}")
    report.line()
    report.line("ИСТОЧНИКИ (collector.adapter_runs):")
    runs = sql("SELECT source_key, status, http_requests, documents_found, documents_new, "
               "coalesce(error_code, ''), coalesce(error_message, '') FROM collector.adapter_runs "
               f"WHERE collection_id = '{collection_id}' ORDER BY source_key") if collection_id else []
    ok, failed = [], []
    for run_row in runs:
        report.line("  " + " | ".join(run_row))
        if len(run_row) >= 2:
            (ok if run_row[1] == "COMPLETED" else failed).append(f"{run_row[0]}:{run_row[1]}")
    summary["sources_ok"] = ", ".join(ok) or "—"
    summary["sources_failed"] = ", ".join(failed) or "—"

    report.line()
    report.line("АНАЛИЗ (analyzer.analyses):")
    columns = ("documents_input", "documents_after_dedup", "clusters_total", "candidates_scored",
               "weak_signals_total", "weak_signals_confident", "excluded_mature", "excluded_hype_or_noise",
               "excluded_insufficient_evidence", "excluded_off_topic", "weak_signal_threshold", "duration_ms",
               "model_version_id")
    analysis = sql(f"SELECT {', '.join(columns)} FROM analyzer.analyses WHERE analysis_id = '{analysis_id}'") \
        if analysis_id else []
    values = dict(zip(columns, analysis[0], strict=False)) if analysis else {}
    for column in columns:
        report.line(f"  {column}: {values.get(column, '—')}")
    summary["clusters"] = values.get("clusters_total", "—")

    status, results = request("GET", f"/api/v1/jobs/{job_id}/results")
    if status != 200 or not isinstance(results, dict):
        report.line(f"результат недоступен: код {status}, ответ {results}")
        return summary
    stats = results.get("stats") or {}
    report.line()
    report.line("СТАТИСТИКА ВЫДАЧИ (stats):")
    for field, value in stats.items():
        report.line(f"  {field}: {value}")
    items = results.get("items") or []
    excluded = results.get("excluded") or []
    summary.update({
        "documents": stats.get("documents_collected", "—"),
        "candidates": stats.get("candidates_found", "—"),
        "signals": stats.get("weak_signals_total", "—"),
        "confident": stats.get("weak_signals_confident", "—"),
        "generated": stats.get("narratives_generated", "—"),
        "fallback": stats.get("narratives_fallback", "—"),
        "items": len(items),
        "off_topic": sum(1 for c in excluded if c.get("decision") == "OFF_TOPIC"),
    })

    dataset_rows = [row for row in dataset if row["domain"] == domain]
    report.line()
    report.line(f"ВЫДАЧА: {len(items)} технологий")
    matches = 0
    for summary_item in items:
        status, item = request("GET", f"/api/v1/results/items/{summary_item['item_id']}")
        if status == 200 and isinstance(item, dict):
            matches += write_item(report, item, dataset_rows)
        else:
            report.line(f"  #{summary_item.get('rank')} {summary_item.get('title_ru')}: "
                        f"карточка недоступна (код {status})")
    summary["matches"] = matches

    report.line()
    report.line(f"ИСКЛЮЧЁННЫЕ КАНДИДАТЫ: {len(excluded)}")
    for candidate in excluded:
        report.line(f"  [{candidate.get('decision')} | {candidate.get('decision_reason')} | "
                    f"скоринг {pct(candidate.get('score'))} | "
                    f"документов {candidate.get('document_count')}] {candidate.get('title_auto')}")
        report.line(f"     {candidate.get('decision_explanation_ru')}")

    report.line()
    report.line(f"СТРОКИ ДАТАСЕТА ПО ТЕМЕ «{domain}» ({len(dataset_rows)}) — для сверки выдачи:")
    for row in dataset_rows:
        report.line(f"  #{row['n']} [стадия: {row['stage']} | балл {row['score']}] {row['tech']}")
    return summary


def main() -> int:
    """Прогон всех тем и итоговая сводка."""
    if not Path("compose.yaml").exists() or not DATASET.exists():
        print("запустите из корня репозитория weak-signals", file=sys.stderr)
        return 2
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = Path(f"analytics-{run_id}.txt")
    report = Report(path)
    dataset = list(csv.DictReader(DATASET.open(encoding="utf-8")))
    report.line(f"АНАЛИТИКА ПО ШЕСТИ ТЕМАМ ДАТАСЕТА — прогон {run_id} UTC, top_n = {TOP_N}")
    report.line(f"датасет: {DATASET} ({len(dataset)} строк)")
    print(f"отчёт: {path}", flush=True)
    if not preflight(report):
        report.title("ПРОГОН ПРЕРВАН: СТЕНД НЕ ГОТОВ")
        report.line("Задания не запускались, чтобы не получить отказы UPSTREAM_UNAVAILABLE.")
        report.line("Дождитесь состояния healthy у analyzer и повторите прогон.")
        report.handle.close()
        return 1

    summaries = []
    for index, (domain, query) in enumerate(THEMES, 1):
        print(f"[{index}/6] {domain}: {query}", flush=True)
        try:
            summaries.append(analyze_theme(report, index, domain, query, run_id, dataset))
        except Exception as error:  # noqa: BLE001 - одна тема не должна обрывать весь прогон
            report.line(f"ОШИБКА СКРИПТА НА ТЕМЕ: {type(error).__name__}: {error}")
            summaries.append({"domain": domain, "query": query, "status": f"ошибка скрипта: {error}"})
        if index < len(THEMES):
            time.sleep(PAUSE_BETWEEN_THEMES)

    report.title("СВОДКА ПО ТЕМАМ")
    header = ("тема", "статус", "время", "докум.", "кластеров", "кандидатов", "сигналов", "увер.>75%",
              "в выдаче", "откл. OFF_TOPIC", "LLM/экстр.", "совп. датасет")
    report.line(" | ".join(header))
    for s in summaries:
        report.line(" | ".join(str(v) for v in (
            s.get("domain"), s.get("status"), s.get("duration", "—"), s.get("documents", "—"), s.get("clusters", "—"),
            s.get("candidates", "—"), s.get("signals", "—"), s.get("confident", "—"), s.get("items", "—"),
            s.get("off_topic", "—"), f"{s.get('generated', '—')}/{s.get('fallback', '—')}", s.get("matches", "—"),
        )))
    report.line()
    report.line("источники по темам:")
    for s in summaries:
        report.line(f"  {s.get('domain')}: успешно — {s.get('sources_ok', '—')}; "
                    f"отказ — {s.get('sources_failed', '—')}")
    numeric = [s for s in summaries if isinstance(s.get("items"), int)]
    if numeric:
        report.line()
        report.line(f"всего технологий в выдаче: {sum(s['items'] for s in numeric)}")
        report.line(f"эвристических совпадений с датасетом: {sum(s.get('matches', 0) for s in numeric)} "
                    "(грубая сверка по основам слов, каждое совпадение требует ручной проверки)")
        generated = sum(s['generated'] for s in numeric if isinstance(s.get('generated'), int))
        fallback = sum(s['fallback'] for s in numeric if isinstance(s.get('fallback'), int))
        if generated + fallback:
            report.line(f"доля инсайтов, написанных моделью: {generated / (generated + fallback) * 100:.0f}% "
                        f"({generated} из {generated + fallback})")
    report.handle.close()
    print(f"готово: {path}", flush=True)
    return 0


KEY = api_key() if Path(".env").exists() else ""

if __name__ == "__main__":
    sys.exit(main())
