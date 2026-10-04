"""Выгрузка снимков со стенда (этап 2): схема базы и путь «выгрузка → повтор».

Базы и Docker в тесте нет, поэтому проверяются две вещи. Первая — все таблицы и столбцы, на которые опирается
`tools/benchmark/freeze.py`, существуют в миграциях сервисов. Вторая — выгрузка, получив ответы базы в формате
`row_to_json`, пишет снимок, который проходит проверку целостности и повторяется с той же выдачей.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import shutil
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FREEZE = ROOT / "tools" / "benchmark" / "freeze.py"

REQUIRED = {
    "orchestrator.jobs": {"job_id", "query_id", "collection_id", "analysis_id", "status"},
    "orchestrator.queries": {"query_id", "query_text", "normalized_text", "requested_top_n"},
    "orchestrator.job_traces": {"job_id", "stage", "payload"},
    "orchestrator.result_items": {"item_id", "job_id", "rank", "candidate_id", "title_ru", "score", "narrative_status",
                                  "description_ru"},
    "orchestrator.result_item_sources": {"item_id", "position", "document_id"},
    "orchestrator.excluded_candidates": {"job_id", "candidate_id"},
    "collector.collections": {"collection_id", "documents_total", "status", "published_since_year", "time_budget_seconds",
                              "max_total_documents", "started_at", "finished_at"},
    "collector.adapter_runs": {"collection_id", "source_key", "status", "http_requests", "documents_found",
                               "documents_new", "error_code"},
    "collector.collection_documents": {"collection_id", "document_id", "source_key", "relevance_rank", "matched_term"},
    "collector.documents": {"document_id", "url", "title", "body_text", "language_code", "source_key", "source_type",
                            "trust_level", "fetched_at", "origin_domain", "published_at", "doi", "citation_count",
                            "engagement_count"},
    "collector.document_events": {"collection_id", "run_id", "seq", "source_key", "matched_term", "event", "url", "title"},
    "collector.raw_responses": {"collection_id", "run_id", "seq", "source_key", "method", "url", "request_body", "outcome",
                                "error_message", "body_gzip", "body_sha256", "body_bytes", "started_at", "duration_ms"},
    "analyzer.analyses": {"analysis_id"},
    "analyzer.candidates": {"candidate_id", "analysis_id", "cluster_index", "rank", "title_auto", "keyphrases", "score",
                            "decision", "decision_reason", "decision_explanation_ru", "document_count", "query_relevance",
                            "predicted_stage", "predicted_trend"},
    "analyzer.candidate_documents": {"candidate_id", "document_id", "similarity", "is_evidence", "snippet", "source_type",
                                     "trust_level", "published_year"},
    "analyzer.candidate_features": {"candidate_id", "feature_name", "value", "contribution"},
    "analyzer.analysis_captures": {"analysis_id", "as_of", "model_version_id", "embedding_model", "query_text", "params",
                                   "config", "funnel", "encyclopedia", "vectors", "vectors_sha256"},
    "analyzer.model_versions": {"model_version_id", "artifact_path"},
    "insight.llm_recordings": {"recording_id", "correlation_id", "replay_key", "provider", "model", "status",
                               "response_text", "temperature", "messages", "max_tokens"},
}


def _schema() -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for path in sorted(ROOT.glob("services/*/migrations/*.sql")):
        text = path.read_text(encoding="utf-8")
        for schema, table, body in re.findall(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\.(\w+)\s*\((.*?)\n\);", text, re.S):
            tables.setdefault(f"{schema}.{table}", set()).update(re.findall(r"^\s{2}(\w+)\s+[a-z]", body, re.M))
        for schema, table, column in re.findall(r"ALTER TABLE (\w+)\.(\w+)\s+ADD COLUMN(?: IF NOT EXISTS)? (\w+)", text):
            tables.setdefault(f"{schema}.{table}", set()).add(column)
    return tables


def _freeze_module():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("freeze_tool", FREEZE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class FreezeSchemaTest(unittest.TestCase):
    def test_every_table_and_column_used_by_freeze_exists(self) -> None:
        schema = _schema()
        for table, columns in REQUIRED.items():
            self.assertIn(table, schema, table)
            self.assertEqual(columns - schema[table], set(), table)
        used = set(re.findall(r"\b((?:orchestrator|collector|analyzer|insight)\.[a-z_]+)\b", FREEZE.read_text(encoding="utf-8")))
        self.assertEqual(used - set(REQUIRED), set(), "freeze.py обращается к таблицам вне проверенного списка")

    def test_candidates_are_read_like_the_analyzer_repository(self) -> None:
        repository = (ROOT / "services/analyzer/src/analyzer/adapters/outbound/postgres/candidate_repository.py").read_text(encoding="utf-8")
        freeze = FREEZE.read_text(encoding="utf-8")
        for expression in ("(CASE WHEN decision = 'WEAK_SIGNAL' THEN 0 ELSE 1 END) AS grp",
                           "(CASE WHEN decision = 'WEAK_SIGNAL' THEN rank::double precision ELSE -score END) AS ord"):
            self.assertIn(expression, repository)
            self.assertIn(expression, freeze)
        self.assertIn("ORDER BY grp, ord, cluster_index", repository)
        self.assertIn("ORDER BY o.grp, o.ord, o.cluster_index", freeze)
        self.assertIn("ORDER BY similarity DESC", repository)
        self.assertIn("ORDER BY t.similarity DESC", freeze)
        page = (ROOT / "services/orchestrator/src/orchestrator/application/stages/narrate.py").read_text(encoding="utf-8")
        self.assertIn("CANDIDATES_PAGE_SIZE = 50", page)
        self.assertIn("PAGE_SIZE = 50", freeze)

    def test_job_text_comes_from_queries(self) -> None:
        text = FREEZE.read_text(encoding="utf-8")
        self.assertIn("JOIN orchestrator.queries q USING (query_id)", text)
        self.assertNotIn("query_text", _schema()["orchestrator.jobs"])


class FreezeEndToEndTest(unittest.TestCase):
    """Ответы базы в формате row_to_json → снимок → проверка → повтор с той же выдачей."""

    def test_freeze_output_verifies_and_replays_identically(self) -> None:
        from ml.benchmark import expert_match, funnel  # noqa: PLC0415
        from ml.benchmark.replay import IDENTICAL, replay  # noqa: PLC0415
        from ml.benchmark.snapshot import load_snapshot  # noqa: PLC0415

        from tests import test_benchmark_replay as base  # noqa: PLC0415

        freeze = _freeze_module()
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            live.mkdir()
            base.live_run(live)
            job = json.loads((live / "job.json").read_text(encoding="utf-8"))
            trace = json.loads((live / "trace.json").read_text(encoding="utf-8"))
            trace |= {"expand": {"ru_terms": [base.TOPIC]}, "collect": {"documents_total": 15}, "analyze": {"status": "COMPLETED"}}
            analysis = json.loads((live / "analysis.json").read_text(encoding="utf-8"))
            capture = json.loads((live / "capture.json").read_text(encoding="utf-8"))
            blob = (live / "vectors.npz").read_bytes()
            rows = [json.loads(line) for line in (live / "documents.jsonl").read_text(encoding="utf-8").splitlines() if line]
            recordings = [json.loads(line) for line in (live / "llm_recordings.jsonl").read_text(encoding="utf-8").splitlines() if line]
            job_row = {"job_id": base.JOB_ID, "query_id": "q-1", "query_text": base.TOPIC, "normalized_text": base.TOPIC,
                       "requested_top_n": 4, "status": "COMPLETED", "collection_id": base.COLLECTION_ID,
                       "analysis_id": base.ANALYSIS_ID}
            answers = [
                ("FROM orchestrator.jobs j JOIN orchestrator.queries q", job_row),
                ("orchestrator.job_traces", trace),
                ("FROM orchestrator.result_items i", job["items"]),
                ("orchestrator.result_item_sources", job["sources"]),
                ("orchestrator.excluded_candidates", []),
                ("FROM collector.collections c", {"collection_id": base.COLLECTION_ID, "status": "COMPLETED",
                                                 "documents_total": len(rows), "published_since_year": 2024}),
                ("collector.adapter_runs", [{"source_key": "openalex", "status": "COMPLETED", "http_requests": 2,
                                             "documents_found": len(rows), "documents_new": len(rows), "error_code": None}]),
                ("collector.collection_documents", rows),
                ("collector.document_events", [{"run_id": "r", "seq": n, "source_key": "openalex", "event": "ACCEPTED",
                                                "url": row["url"], "title": row["title"], "matched_term": row["matched_term"]}
                                               for n, row in enumerate(rows, 1)]),
                ("collector.raw_responses", [{"run_id": "r", "seq": 1, "source_key": "openalex", "outcome": "OK"}]),
                ("FROM analyzer.analyses a", {"analysis_id": base.ANALYSIS_ID, "status": "COMPLETED"}),
                ("FROM analyzer.candidates ca", analysis["candidates"]),
                ("FROM analyzer.analysis_captures", capture | {"vectors_sha256": hashlib.sha256(blob).hexdigest()}),
                ("insight.llm_recordings", recordings),
            ]

            def psql_json(query: str):  # noqa: ANN202
                for marker, answer in answers:
                    if marker in query:
                        return answer
                raise AssertionError(f"неожиданный запрос выгрузки: {query[:120]}")

            lines = {name: [json.loads(line) for line in (live / name).read_text(encoding="utf-8").splitlines() if line]
                     for name in ("candidate_rows.jsonl", "candidate_features.jsonl", "candidate_documents.jsonl")}

            def psql_rows(query: str) -> list:
                if "FROM analyzer.candidates c WHERE c.analysis_id" in query:
                    return lines["candidate_rows.jsonl"]
                if "FROM analyzer.candidate_features t" in query:
                    return lines["candidate_features.jsonl"]
                if "FROM analyzer.candidate_documents t" in query:
                    return lines["candidate_documents.jsonl"]
                raise AssertionError(f"неожиданный запрос выгрузки: {query[:120]}")

            def psql_text(query: str) -> str:
                if "encode(vectors" in query:
                    return base64.encodebytes(blob).decode()
                if "artifact_path" in query:
                    return "active\n"
                raise AssertionError(f"неожиданный запрос выгрузки: {query[:120]}")

            sources = {"/models/active": live / "model" / "active",
                       "/app/services/insight/prompts": ROOT / "services/insight/prompts",
                       "/app/services/insight/schemas": ROOT / "services/insight/schemas",
                       "/app/services/orchestrator/config": ROOT / "services/orchestrator/config"}

            def compose_cp(service: str, source: str, target: Path) -> str:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(sources[source], target)
                return ""

            freeze.psql_json, freeze.psql_text, freeze.compose_cp, freeze.psql_rows = psql_json, psql_text, compose_cp, psql_rows
            freeze.compose_env = lambda service, name: "/models"
            freeze.subprocess = types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(stdout="", stderr="", returncode=0),
                                                      SubprocessError=Exception)
            root, issues = freeze.freeze_job(base.JOB_ID, Path(tmp) / "out")
            self.assertEqual(issues, [])
            snapshot = load_snapshot(root)
            self.assertEqual(snapshot.topic, base.TOPIC)
            self.assertEqual(snapshot.verify(), [])
            result = replay(snapshot)
            self.assertEqual(result.status, IDENTICAL, result.differences or result.issues or result.misses)
            self.assertEqual(result.analysis_mode, "recorded")
            self.assertEqual(funnel.funnel(snapshot)["collection"]["sources"]["openalex"]["outcome"], funnel.OK)
            self.assertIsNone(expert_match.match([snapshot], [], {})["runs"][0]["domain"])


if __name__ == "__main__":
    unittest.main()
