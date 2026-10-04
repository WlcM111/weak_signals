#!/usr/bin/env python3
"""Выгрузка снимков заданий для режима А (аудит A3, этап 2). Запуск из корня проекта на машине со стендом.

  python3 tools/benchmark/freeze.py --run-file ml/reports/stand/stand-run-….json --out ml/reports/benchmark
  python3 tools/benchmark/freeze.py --job-id <uuid> [--job-id …] --out ml/reports/benchmark

Только стандартная библиотека. Чтение — SELECT через `docker compose exec postgres psql`, файлы моделей и промптов —
`docker compose cp` из работающих контейнеров (ровно те версии, на которых выполнялось задание). Секреты .env в снимок
не попадают; адреса запросов к источникам уже записаны без ключей. Каждый файл снимка — с SHA-256 в manifest.json.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCHEMA = "ws-benchmark-snapshot/1"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS|AUTH)", re.IGNORECASE)
TRACE_STAGES = ("job", "expand", "collect", "analyze", "narrate")
# Кандидаты читаются так же, как их читает analyzer для orchestrator (PostgresCandidateRepository.list_candidates):
# страницы по CANDIDATES_PAGE_SIZE = 50 в порядке (grp, ord, cluster_index), документы — одним запросом на страницу
# с ORDER BY similarity DESC. Порядок документов с равной близостью задаёт PostgreSQL, и повтор берёт именно его.
PAGE_SIZE = 50
CANDIDATES_ORDERED = (
    "SELECT row_to_json(o) FROM (SELECT c.*, "
    "(CASE WHEN decision = 'WEAK_SIGNAL' THEN 0 ELSE 1 END) AS grp, "
    "(CASE WHEN decision = 'WEAK_SIGNAL' THEN rank::double precision ELSE -score END) AS ord "
    "FROM analyzer.candidates c WHERE c.analysis_id = '{analysis_id}') o ORDER BY o.grp, o.ord, o.cluster_index"
)


def psql_json(query: str):  # noqa: ANN201
    """Результат запроса, вернувшего одно значение JSON; None для пустого ответа."""
    result = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "weaksignals",
                             "-v", "ON_ERROR_STOP=1", "-At", "-c", query], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"psql: {result.stderr.strip()[:300]}")
    text = result.stdout.strip()
    return json.loads(text) if text else None


def psql_rows(query: str) -> list:
    """Строки результата по одной на строку вывода (порядок строк — порядок запроса)."""
    return [json.loads(line) for line in psql_text(query).splitlines() if line.strip()]


def candidate_pages(analysis_id: str) -> tuple[list, list, list]:
    """Кандидаты, их признаки и документы в том порядке, в каком их получил живой orchestrator."""
    rows = psql_rows(CANDIDATES_ORDERED.format(analysis_id=analysis_id))
    features, documents = [], []
    for start in range(0, len(rows), PAGE_SIZE):
        ids = ",".join(f"'{row['candidate_id']}'" for row in rows[start : start + PAGE_SIZE])
        array = f"ARRAY[{ids}]::uuid[]"
        features += psql_rows(f"SELECT row_to_json(t) FROM analyzer.candidate_features t WHERE t.candidate_id = ANY({array})")
        documents += psql_rows("SELECT row_to_json(t) FROM analyzer.candidate_documents t "
                               f"WHERE t.candidate_id = ANY({array}) ORDER BY t.similarity DESC")
    return rows, features, documents


def psql_text(query: str) -> str:
    result = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "weaksignals",
                             "-v", "ON_ERROR_STOP=1", "-At", "-c", query], capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"psql: {result.stderr.strip()[:300]}")
    return result.stdout


def compose_cp(service: str, source: str, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["docker", "compose", "cp", f"{service}:{source}", str(target)], capture_output=True, text=True,
                            timeout=300)
    return "" if result.returncode == 0 else f"{service}:{source}: {(result.stderr or result.stdout).strip()[:200]}"


def compose_env(service: str, name: str) -> str:
    result = subprocess.run(["docker", "compose", "exec", "-T", service, "printenv", name], capture_output=True, text=True,
                            timeout=60)
    return result.stdout.strip()


def env_without_secrets(path: Path) -> dict[str, str]:
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = "***" if SECRET_RE.search(key) else value.strip()
    return values


def git_state() -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""
    head = run("rev-parse", "HEAD")
    return {"head": head or "нет данных", "dirty_files": str(len([l for l in run("status", "--porcelain").splitlines() if l]))
            if head else "нет данных"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(root: Path, manifest: dict) -> dict:
    files = {str(p.relative_to(root)): {"sha256": sha256_file(p), "bytes": p.stat().st_size}
             for p in sorted(x for x in root.rglob("*") if x.is_file() and x.name != "manifest.json")}
    manifest = dict(manifest, schema=SCHEMA, files=files)
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
    return manifest


def dump(path: Path, payload) -> None:  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def dump_lines(path: Path, rows) -> None:  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows or []), encoding="utf-8")


def slug(text: str) -> str:
    """Имя каталога снимка: транслитерация темы латиницей."""
    table = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh shch _ y _ e yu ya".split()))
    latin = "".join(table.get(c, c).replace("_", "") for c in text.lower())
    return re.sub(r"[^a-z0-9]+", "-", latin).strip("-")[:40] or "topic"


def freeze_job(job_id: str, out: Path) -> tuple[Path, list[str]]:
    """Выгружает одно задание; возвращает каталог снимка и список проблем полноты."""
    if not UUID_RE.match(job_id):
        raise ValueError(f"не UUID: {job_id}")
    issues: list[str] = []
    # Текст запроса и требуемое число карточек хранятся в orchestrator.queries, а не в jobs.
    job = psql_json("SELECT row_to_json(x) FROM (SELECT j.*, q.query_text, q.normalized_text, q.requested_top_n "
                    "FROM orchestrator.jobs j JOIN orchestrator.queries q USING (query_id) "
                    f"WHERE j.job_id = '{job_id}') x")
    if not job:
        raise ValueError(f"задание {job_id} не найдено")
    root = out / f"{slug(job['query_text'])}-{job_id[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    traces = psql_json(f"SELECT coalesce(json_object_agg(stage, payload), '{{}}'::json) FROM orchestrator.job_traces "
                       f"WHERE job_id = '{job_id}'") or {}
    issues += [f"нет трассы стадии {stage}" for stage in TRACE_STAGES if stage not in traces]
    dump(root / "trace.json", traces)
    items = psql_json(f"SELECT coalesce(json_agg(row_to_json(i) ORDER BY i.rank), '[]'::json) FROM orchestrator.result_items i "
                      f"WHERE i.job_id = '{job_id}'")
    sources = psql_json("SELECT coalesce(json_agg(row_to_json(s)), '[]'::json) FROM orchestrator.result_item_sources s "
                        f"WHERE s.item_id IN (SELECT item_id FROM orchestrator.result_items WHERE job_id = '{job_id}')")
    excluded = psql_json("SELECT coalesce(json_agg(row_to_json(e)), '[]'::json) FROM orchestrator.excluded_candidates e "
                         f"WHERE e.job_id = '{job_id}'")
    dump(root / "job.json", {"job": job, "items": items, "sources": sources, "excluded": excluded})
    collection_id, analysis_id = job.get("collection_id"), job.get("analysis_id")
    if collection_id:
        collection = psql_json(f"SELECT row_to_json(c) FROM collector.collections c WHERE collection_id = '{collection_id}'")
        runs = psql_json("SELECT coalesce(json_agg(row_to_json(r) ORDER BY r.source_key), '[]'::json) FROM collector.adapter_runs r "
                         f"WHERE r.collection_id = '{collection_id}'")
        dump(root / "collection.json", {"collection": collection, "adapter_runs": runs})
        documents = psql_json(
            "SELECT coalesce(json_agg(row_to_json(x) ORDER BY x.relevance_rank, x.document_id), '[]'::json) FROM ("
            "SELECT cd.relevance_rank, cd.matched_term, cd.source_key AS link_source_key, d.* "
            "FROM collector.collection_documents cd JOIN collector.documents d USING (document_id) "
            f"WHERE cd.collection_id = '{collection_id}') x")
        dump_lines(root / "documents.jsonl", documents)
        if collection and len(documents or []) != int(collection.get("documents_total") or 0):
            issues.append(f"документов выгружено {len(documents or [])}, в коллекции {collection.get('documents_total')}")
        events = psql_json("SELECT coalesce(json_agg(row_to_json(e) ORDER BY e.run_id, e.seq), '[]'::json) "
                           f"FROM collector.document_events e WHERE e.collection_id = '{collection_id}'")
        dump_lines(root / "document_events.jsonl", events)
        responses = psql_json(
            "SELECT coalesce(json_agg(json_build_object('run_id', r.run_id, 'seq', r.seq, 'source_key', r.source_key, "
            "'method', r.method, 'url', r.url, 'request_body', r.request_body, 'outcome', r.outcome, 'error_message', "
            "r.error_message, 'body_sha256', r.body_sha256, 'body_bytes', r.body_bytes, 'started_at', r.started_at, "
            "'duration_ms', r.duration_ms, 'body_gzip_base64', encode(r.body_gzip, 'base64')) ORDER BY r.run_id, r.seq), "
            f"'[]'::json) FROM collector.raw_responses r WHERE r.collection_id = '{collection_id}'")
        dump_lines(root / "raw_responses.jsonl", responses)
        if not events:
            issues.append("нет событий документов: трасса сбора выключена или не записана")
        if not responses:
            issues.append("нет сырых ответов источников: трасса сбора выключена или не записана")
    else:
        issues.append("у задания нет коллекции")
    if analysis_id:
        analysis = psql_json(f"SELECT row_to_json(a) FROM analyzer.analyses a WHERE analysis_id = '{analysis_id}'") or {}
        candidates = psql_json(
            "SELECT coalesce(json_agg(row_to_json(c) ORDER BY c.cluster_index), '[]'::json) FROM (SELECT ca.*, "
            "(SELECT coalesce(json_agg(cd.document_id ORDER BY cd.similarity DESC), '[]'::json) FROM analyzer.candidate_documents cd "
            f"WHERE cd.candidate_id = ca.candidate_id) AS document_ids FROM analyzer.candidates ca WHERE ca.analysis_id = '{analysis_id}') c")
        dump(root / "analysis.json", {"analysis_id": analysis_id, "collection_id": collection_id, "analysis": analysis,
                                      "candidates": candidates})
        rows, features, documents = candidate_pages(analysis_id)
        dump_lines(root / "candidate_rows.jsonl", rows)
        dump_lines(root / "candidate_features.jsonl", features)
        dump_lines(root / "candidate_documents.jsonl", documents)
        if len(rows) != len(candidates or []):
            issues.append(f"кандидатов в постраничной выгрузке {len(rows)}, в анализе {len(candidates or [])}")
        capture = psql_json(
            "SELECT json_build_object('as_of', as_of, 'model_version_id', model_version_id, 'embedding_model', embedding_model, "
            "'query_text', query_text, 'params', params, 'config', config, 'funnel', funnel, 'encyclopedia', encyclopedia, "
            f"'vectors_sha256', vectors_sha256) FROM analyzer.analysis_captures WHERE analysis_id = '{analysis_id}'")
        if capture:
            dump(root / "capture.json", capture)
            blob = base64.b64decode(psql_text(f"SELECT encode(vectors, 'base64') FROM analyzer.analysis_captures "
                                              f"WHERE analysis_id = '{analysis_id}'"))
            (root / "vectors.npz").write_bytes(blob)
            if hashlib.sha256(blob).hexdigest() != capture.get("vectors_sha256"):
                issues.append("SHA-256 векторов не совпала с записью analyzer")
            artifact = psql_text("SELECT artifact_path FROM analyzer.model_versions WHERE model_version_id = "
                                 f"'{capture['model_version_id']}'").strip()
            store = compose_env("analyzer", "WS_MODEL_STORE_DIR") or "/models"
            source = artifact if artifact.startswith("/") else f"{store.rstrip('/')}/{artifact}"
            name = Path(source.rstrip("/")).name or capture["model_version_id"]
            problem = compose_cp("analyzer", source, root / "model" / name) if artifact else "путь модели не найден"
            if problem:
                issues.append(f"модель analyzer не скопирована: {problem}")
        else:
            issues.append("нет записи входов анализа (analysis_captures): запись выключена или задание старше этапа 2")
    else:
        issues.append("у задания нет анализа")
    recordings = psql_json("SELECT coalesce(json_agg(row_to_json(r) ORDER BY r.recording_id), '[]'::json) "
                           f"FROM insight.llm_recordings r WHERE r.correlation_id = '{job_id}'")
    dump_lines(root / "llm_recordings.jsonl", recordings)
    if not recordings:
        issues.append("нет записей LLM для задания (WS_LLM_RECORD_ENABLED или correlation id)")
    for service, source, target in (("insight", "/app/services/insight/prompts", "insight/prompts"),
                                    ("insight", "/app/services/insight/schemas", "insight/schemas"),
                                    ("orchestrator-worker", "/app/services/orchestrator/config", "orchestrator_config")):
        problem = compose_cp(service, source, root / target)
        if problem:
            issues.append(f"не скопировано: {problem}")
    images = subprocess.run(["docker", "compose", "images"], capture_output=True, text=True, timeout=60).stdout
    collection_row = (json.loads((root / "collection.json").read_text(encoding="utf-8")).get("collection") or {}
                      if (root / "collection.json").is_file() else {})
    manifest = {
        "job_id": job_id, "topic": job["query_text"], "frozen_at": datetime.now().astimezone().isoformat(),
        "job_status": job.get("status"), "collection_started_at": collection_row.get("started_at"),
        "collection_finished_at": collection_row.get("finished_at"), "versions": {"git": git_state(),
        "compose_images": images.strip().splitlines(), "prompt_versions": sorted({r.get("prompt_version") for r in recordings or []}),
        "llm_models": sorted({f"{r.get('provider')}:{r.get('model')}" for r in recordings or []})},
        "env": env_without_secrets(Path(".env")), "issues": issues,
    }
    write_manifest(root, manifest)
    return root, issues


def job_ids(run_file: Path) -> list[str]:
    blocks = json.loads(run_file.read_text(encoding="utf-8"))
    return [str((block.get("job") or {}).get("job_id")) for block in blocks if (block.get("job") or {}).get("job_id")]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="выгрузка снимков заданий для режима А")
    parser.add_argument("--run-file", action="append", default=[])
    parser.add_argument("--job-id", action="append", default=[])
    parser.add_argument("--out", default="ml/reports/benchmark")
    args = parser.parse_args(argv)
    ids = list(args.job_id) + [job for path in args.run_file for job in job_ids(Path(path))]
    if not ids:
        print("нет заданий: укажите --run-file или --job-id", file=sys.stderr)
        return 2
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    failed = 0
    for job_id in ids:
        try:
            root, issues = freeze_job(job_id, out)
        except Exception as error:  # noqa: BLE001 - сбой одного задания не должен останавливать выгрузку остальных
            failed += 1
            print(f"{job_id}: ОШИБКА {type(error).__name__}: {error}")
            continue
        print(f"{job_id}: {root} — " + ("полный" if not issues else "проблемы: " + "; ".join(issues)))
    print(f"снимки: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
