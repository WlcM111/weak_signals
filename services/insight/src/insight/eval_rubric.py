"""Оценка рубричного судьи на размеченных данных (шаги 1–3 переработки отбора).

Датасет A — 100 слабых сигналов, размеченных методологами организаторов (стадия 1–4, тренд 1–3), и 125
негативов команды: фильтр (R или нет) сравнивается с меткой, стадия и тренд — с разметкой организаторов,
балл «стадия + тренд» — с их баллом. Датасет B v2 — карточки живой выдачи с silver-метками (оценка LLM по той же
рубрике, поэтому сравнение с ними — ориентир, а не независимая проверка). Вызовы идут через ту же цепочку
провайдеров, что и в сервисе, и журналируются в insight.llm_calls (ТЗ: явное логирование модели).

Запуск в контейнере insight (данные примонтированы в /eval-data, отчёты пишутся в /eval-out):
  python -m insight.eval_rubric --dataset a --input /eval-data/labels/dataset_ds-2026.09.19-v2.jsonl --out /eval-out
  python -m insight.eval_rubric --dataset b --input /eval-data/dataset_b/v2/dataset_b_v2.jsonl \
      --input /eval-data/dataset_b/v2/dataset_b_v2_uncertain.jsonl --out /eval-out
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from insight.application.prompt_builder import RUBRIC_PROMPT_VERSION
from insight.application.use_cases.rubric_judge import MAX_ITEMS, RubricItem, RubricSource, RubricVerdict

SCIENCE = ("arxiv.org", "nature.com", "sciencedirect.com", "springer.com", "ieee.org", "acm.org", "doi.org",
           "science.org", "mdpi.com", "wiley.com", "biorxiv.org", "medrxiv.org")
PRESS = ("prnewswire.com", "businesswire.com", "globenewswire.com")
# Домен датасета A → запрос кейса (как в tools/theme_analytics.py): критерий «по теме» оценивается честно.
DOMAIN_QUERY = {
    "industrial_ai": "индустриальный искусственный интеллект на производстве",
    "robotics": "робототехника и физический искусственный интеллект",
    "ai_infrastructure": "инфраструктура для искусственного интеллекта и дата-центры",
    "fintech": "перспективные решения в финтехе",
    "ai_security": "защита и безопасность систем искусственного интеллекта",
    "edge": "edge-вычисления и периферийный инференс",
}


def domain(url: str) -> str:
    return (urlsplit(url or "").hostname or "").removeprefix("www.")


def source_type(host: str) -> tuple[str, str]:
    """Тип и доверенность источника строки A по домену (приближение правил collector)."""
    if host == "arxiv.org":
        return "PREPRINT", "HIGH"
    if any(host == item or host.endswith("." + item) for item in SCIENCE):
        return "SCIENTIFIC_PUBLICATION", "HIGH"
    if host.endswith("wikipedia.org"):
        return "ENCYCLOPEDIA", "MEDIUM"
    if host.endswith("github.com"):
        return "CODE_REPOSITORY", "LOW"
    if any(host.endswith(item) for item in PRESS):
        return "PRESS_RELEASE", "LOW"
    return "INDUSTRY_MEDIA", "MEDIUM"


def a_item(row: dict) -> RubricItem:
    """Строка датасета A → кандидат: название, описание как фрагмент первого источника, ссылки как источники."""
    urls = [url for url in row.get("source_urls") or [] if url][:6] or [""]
    sources = []
    for index, url in enumerate(urls):
        host = domain(url)
        kind, trust = source_type(host) if host else ("OTHER", "LOW")
        sources.append(RubricSource(document_id=f"{row['row_id']}-{index}", title=row["title"], source_type=kind,
                                    trust_level=trust, language_code="ru",
                                    snippet=row.get("description", "") if index == 0 else "", domain=host))
    return RubricItem(row["row_id"], row["title"], (), tuple(sources),
                      f"Источников {len(sources)}: " + ", ".join(sorted({s.domain or 'без ссылки' for s in sources})))


def b_item(row: dict) -> RubricItem:
    """Строка датасета B v2 → кандидат: название карточки и её доказательства (название, тип, дата, площадка)."""
    refs = row.get("evidence_refs") or []
    sources = tuple(
        RubricSource(document_id=f"{row['group_id']}-{index}", title=ref.get("title", ""),
                     source_key=ref.get("source_key", ""), source_type=ref.get("source_type", ""),
                     trust_level=ref.get("trust_level", ""), published=(ref.get("published_at") or "")[:10],
                     language_code=ref.get("language_code", ""),
                     snippet=(row.get("observation_text") or "")[:400] if index == 0 else "",
                     domain=domain(ref.get("url", "")))
        for index, ref in enumerate(refs[:6])
    )
    return RubricItem(row["group_id"], row.get("title", ""), (), sources, "")


def classification(y_true: Sequence[int], y_pred: Sequence[int]) -> dict:
    tp = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 1)
    fp = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 1)
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == 1 and p == 0)
    tn = sum(1 for t, p in zip(y_true, y_pred) if t == 0 and p == 0)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    total = tp + fp + fn + tn
    return {"n": total, "accuracy": round((tp + tn) / total, 4) if total else None, "precision": round(precision, 4),
            "recall": round(recall, 4), "f1": round(f1, 4), "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def qwk(true: Sequence[int], pred: Sequence[int], low: int, high: int) -> float | None:
    """Квадратично взвешенная каппа Коэна для порядковых меток."""
    k = high - low + 1
    if not true or k < 2:
        return None
    observed = [[0.0] * k for _ in range(k)]
    for t, p in zip(true, pred):
        observed[t - low][p - low] += 1
    n = len(true)
    row = [sum(observed[i]) for i in range(k)]
    col = [sum(observed[i][j] for i in range(k)) for j in range(k)]
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            weight = (i - j) ** 2 / (k - 1) ** 2
            num += weight * observed[i][j]
            den += weight * row[i] * col[j] / n
    return round(1 - num / den, 4) if den else None


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    def ranks(values: Sequence[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for m in range(i, j + 1):
                result[order[m]] = (i + j) / 2 + 1
            i = j + 1
        return result
    if len(x) < 3:
        return None
    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return round(cov / (sx * sy), 4) if sx and sy else None


def ordinal(true: Sequence[int], pred: Sequence[int], low: int, high: int) -> dict:
    """Точность, MAE и каппа порядковой оценки и те же метрики «самого частого класса»."""
    if not true:
        return {"n": 0}
    majority = Counter(true).most_common(1)[0][0]
    return {"n": len(true), "accuracy": round(sum(t == p for t, p in zip(true, pred)) / len(true), 4),
            "mae": round(sum(abs(t - p) for t, p in zip(true, pred)) / len(true), 4), "qwk": qwk(true, pred, low, high),
            "majority_class": majority, "majority_accuracy": round(sum(t == majority for t in true) / len(true), 4),
            "majority_mae": round(sum(abs(t - majority) for t in true) / len(true), 4),
            "confusion": {f"{t}->{p}": c for (t, p), c in sorted(Counter(zip(true, pred)).items())}}


def summarize(dataset: str, rows: list[dict], verdicts: dict[str, RubricVerdict]) -> tuple[dict, list[dict]]:
    """Метрики и построчные предсказания."""
    key = "row_id" if dataset == "a" else "group_id"
    predictions: list[dict] = []
    y_true: list[int] = []
    y_pred: list[int] = []
    stage_t, stage_p, trend_t, trend_p, score_t, score_p = [], [], [], [], [], []
    for row in rows:
        verdict = verdicts.get(row[key])
        label = row.get("label")
        predictions.append({
            "id": row[key], "split": row.get("split", ""), "label": label, "label_kind": row.get("label_kind", ""),
            "pred_code": verdict.code if verdict else "", "stage_true": row.get("stage_ordinal") or "",
            "stage_pred": verdict.stage if verdict else "", "trend_true": row.get("trend_ordinal") or "",
            "trend_pred": verdict.trend if verdict else "", "confidence": verdict.confidence if verdict else "",
            "reason_ru": verdict.reason_ru if verdict else "", "title": row.get("title", "")[:200],
            "topic": row.get("topic", row.get("domain_tag", "")),
            "technology_ru": verdict.technology_ru if verdict else "", "profile_ru": verdict.profile_ru if verdict else ""})
        if verdict is None or label is None:
            continue
        y_true.append(int(label))
        y_pred.append(1 if verdict.code == "R" else 0)
        if dataset == "a" and int(label) == 1 and row.get("stage_ordinal") and row.get("trend_ordinal"):
            stage_t.append(int(row["stage_ordinal"]))
            stage_p.append(verdict.stage)
            trend_t.append(int(row["trend_ordinal"]))
            trend_p.append(verdict.trend)
            score_t.append(int(row["stage_ordinal"]) + int(row["trend_ordinal"]))
            score_p.append(verdict.stage + verdict.trend)
    labeled = sum(1 for row in rows if row.get("label") is not None)
    metrics = {"dataset": dataset, "rows": len(rows), "labeled": labeled, "judged": len(y_true),
               "coverage": round(len(y_true) / labeled, 4) if labeled else None,
               "filter": classification(y_true, y_pred),
               "codes": dict(Counter(v.code for v in verdicts.values()).most_common())}
    if dataset == "a":
        metrics["stage"] = ordinal(stage_t, stage_p, 1, 4)
        metrics["trend"] = ordinal(trend_t, trend_p, 1, 3)
        metrics["score"] = {"n": len(score_t), "spearman": spearman(score_t, score_p),
                            "mae": round(sum(abs(a - b) for a, b in zip(score_t, score_p)) / len(score_t), 4)
                            if score_t else None}
    else:
        metrics["by_split"] = {}
        for split in ("dev", "holdout"):
            pairs = [(int(r["label"]), 1 if verdicts[r[key]].code == "R" else 0) for r in rows
                     if r.get("label") is not None and r[key] in verdicts and str(r.get("split", "")).startswith(split)]
            metrics["by_split"][split] = classification([t for t, _ in pairs], [p for _, p in pairs])
    return metrics, predictions


def grouped(dataset: str, rows: list[dict], items: list[RubricItem]) -> dict[str, list[RubricItem]]:
    """Кандидаты по запросу: для A — запрос кейса по домену строки, для B — тема карточки."""
    groups: dict[str, list[RubricItem]] = {}
    for row, item in zip(rows, items, strict=True):
        query = DOMAIN_QUERY.get(row.get("domain_tag", ""), "") if dataset == "a" else row.get("topic", "")
        groups.setdefault(query or "технологические слабые сигналы", []).append(item)
    return groups


def calibrated(verdicts: dict[str, RubricVerdict], stage_spec: str, trend_spec: str) -> dict[str, RubricVerdict]:
    """Та же калибровка шкалы, что в orchestrator (WS_STAGE_CALIBRATION, WS_TREND_CALIBRATION)."""
    from dataclasses import replace  # noqa: PLC0415

    def mapping(spec: str, low: int, high: int) -> dict[int, int]:
        result = {value: value for value in range(low, high + 1)}
        for part in (spec or "").split(","):
            if ":" in part and all(x.strip().isdigit() for x in part.split(":", 1)):
                source, target = (int(x) for x in part.split(":", 1))
                if low <= source <= high:
                    result[source] = min(max(target, low), high)
        return result
    stages, trends = mapping(stage_spec, 1, 4), mapping(trend_spec, 1, 3)
    return {key: replace(v, stage=stages[v.stage], trend=trends[v.trend]) for key, v in verdicts.items()}


def load(paths: Sequence[str]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        rows.extend(json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip())
    return rows


async def run(args: argparse.Namespace) -> int:
    from insight.adapters.outbound.metrics import PrometheusMetrics  # noqa: PLC0415 - рабочий контур
    from insight.adapters.outbound.postgres.repositories import PostgresLLMCallLog, PostgresPromptRegistry  # noqa: PLC0415
    from insight.application.prompt_builder import PromptBuilder  # noqa: PLC0415
    from insight.application.provider_chain import ChainConfig, ProviderChain  # noqa: PLC0415
    from insight.application.use_cases.get_provider_status import RegisterPrompts  # noqa: PLC0415
    from insight.application.use_cases.rubric_judge import RubricJudge  # noqa: PLC0415
    from insight.config import InsightSettings  # noqa: PLC0415
    from insight.main import build_providers  # noqa: PLC0415
    from ws_common.clock import SystemClock  # noqa: PLC0415
    from ws_common.config import load_settings  # noqa: PLC0415
    from ws_common.db import build_pool  # noqa: PLC0415
    from ws_common.logging import configure_logging  # noqa: PLC0415

    settings = load_settings(InsightSettings)
    configure_logging(settings.service_name, settings.log_level, settings.log_format)
    rows = load(args.input)
    if args.limit:
        rows = rows[: args.limit]
    items = [a_item(row) if args.dataset == "a" else b_item(row) for row in rows]
    pool = build_pool(settings.pg_dsn, min_size=1, max_size=2, statement_timeout_ms=settings.pg_statement_timeout_ms,
                      application_name=f"{settings.service_name}-eval")
    await pool.open(wait=True, timeout=30)
    prompts = PromptBuilder(settings.prompts_dir, settings.schemas_dir)
    await RegisterPrompts(PostgresPromptRegistry(pool), prompts).execute()
    chain = ProviderChain(build_providers(settings), PostgresLLMCallLog(pool), SystemClock(), ChainConfig(
        timeout_seconds=settings.llm_timeout_seconds, temperature=settings.llm_temperature,
        circuit_breaker_failures=settings.circuit_breaker_failures,
        circuit_breaker_cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        daily_token_budget=settings.llm_daily_token_budget, total_budget_seconds=settings.llm_total_budget_seconds),
        PrometheusMetrics())
    judge = RubricJudge(chain, prompts)
    verdicts: dict[str, RubricVerdict] = {}
    provider = model = ""
    for query, group in grouped(args.dataset, rows, items).items():
        for start in range(0, len(group), MAX_ITEMS):
            outcome = await judge.execute(args.query or query, group[start : start + MAX_ITEMS])
            verdicts.update({verdict.candidate_id: verdict for verdict in outcome.verdicts})
            provider, model = outcome.provider or provider, outcome.model or model
            print(f"оценено {len(verdicts)} из {len(items)} ({query[:40]})", flush=True)
    await pool.close()
    verdicts = calibrated(verdicts, args.stage_calibration, args.trend_calibration)
    metrics, predictions = summarize(args.dataset, rows, verdicts)
    metrics.update({"provider": provider, "model": model, "prompt_version": RUBRIC_PROMPT_VERSION, "label": args.label})
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"rubric_{args.dataset}_{args.label}"
    (out / f"{stem}_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / f"{stem}_predictions.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(predictions[0].keys()) if predictions else ["id"])
        writer.writeheader()
        writer.writerows(predictions)
    print(json.dumps({k: metrics[k] for k in metrics if k not in ("codes",)}, ensure_ascii=False, indent=1))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="оценка рубричного судьи на размеченных данных")
    parser.add_argument("--dataset", choices=("a", "b"), required=True)
    parser.add_argument("--input", action="append", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default="default", help="метка прогона в имени файлов (например, модель)")
    parser.add_argument("--query", default="", help="запрос для оценки A (по умолчанию общий)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--stage-calibration", default="", help='калибровка стадии, например "1:2,2:3,3:4,4:4"')
    parser.add_argument("--trend-calibration", default="", help='калибровка тренда, например "1:2,2:3,3:3"')
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
