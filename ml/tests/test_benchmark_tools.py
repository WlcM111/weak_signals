"""Воронка, сопоставление с экспертами, статистика и выгрузка снимков (этап 2, аудит A3/A4, B2-a)."""

from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from analyzer.application.capture import AnalysisCapture, CapturingEmbedder, pack_vectors, unpack_vectors
from ml.benchmark import expert_match, funnel, stats
from ml.benchmark.snapshot import load_snapshot, write_manifest

ROOT = Path(__file__).resolve().parents[2]


def _freeze_module():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("freeze", ROOT / "tools/benchmark/freeze.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class SourceOutcomeTest(unittest.TestCase):
    """B2-a: «не опрошен» не смешивается с «опрошен, пусто»."""

    def test_outcomes(self) -> None:
        o = funnel.source_outcome
        self.assertEqual(o("COMPLETED", 0, 0, None), funnel.NOT_POLLED)  # завершён без запросов — не опрошен
        self.assertEqual(o("FAILED", 0, 0, "AUTH_MISSING"), funnel.NOT_POLLED)
        self.assertEqual(o("COMPLETED", 3, 0, None), funnel.EMPTY)
        self.assertEqual(o("COMPLETED", 3, 7, None), funnel.OK)
        self.assertEqual(o("FAILED", 2, 0, "RATE_LIMITED"), funnel.RATE_LIMITED)
        self.assertEqual(o("FAILED", 2, 5, "RATE_LIMITED"), funnel.PARTIAL)
        self.assertEqual(o("COMPLETED", 4, 0, "BUDGET_EXHAUSTED"), funnel.INCOMPLETE)
        self.assertEqual(o("FAILED", 1, 0, "TIMEOUT"), funnel.FAILED)
        self.assertEqual(o("CANCELLED", 1, 2, None), funnel.PARTIAL)


def _snapshot(root: Path, topic: str = "перспективные решения в финтехе") -> Path:
    docs = [{"document_id": "d1", "relevance_rank": 1, "title": "Agent-to-agent payments protocol", "body_text": "агентские платежи",
             "url": "https://arxiv.org/abs/2511.15712v1", "doi": None},
            {"document_id": "d2", "relevance_rank": 2, "title": "Обзор рынка", "body_text": "", "url": "https://x.org/a", "doi": None},
            {"document_id": "d3", "relevance_rank": 3, "title": "Нерелевантное", "body_text": "", "url": "https://y.org/b", "doi": None}]
    (root / "documents.jsonl").write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs), encoding="utf-8")
    (root / "document_events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in [
        {"run_id": "r", "seq": 1, "source_key": "arxiv", "event": "ACCEPTED", "url": docs[0]["url"], "title": docs[0]["title"]},
        {"run_id": "r", "seq": 2, "source_key": "openalex", "event": "DEDUP", "url": docs[0]["url"], "title": docs[0]["title"]},
        {"run_id": "r", "seq": 3, "source_key": "openalex", "event": "REJECTED", "url": "https://z.org", "title": "Sponsored"}]),
        encoding="utf-8")
    (root / "raw_responses.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"source_key": "arxiv", "outcome": "OK"}, {"source_key": "openalex", "outcome": "RATE_LIMITED"},
        {"source_key": "openalex", "outcome": "OK"}]), encoding="utf-8")
    (root / "collection.json").write_text(json.dumps({"collection": {"documents_total": 3, "status": "PARTIAL"}, "adapter_runs": [
        {"source_key": "arxiv", "status": "COMPLETED", "http_requests": 2, "documents_found": 1, "error_code": None},
        {"source_key": "github", "status": "COMPLETED", "http_requests": 0, "documents_found": 0, "error_code": None},
        {"source_key": "zenodo", "status": "COMPLETED", "http_requests": 1, "documents_found": 0, "error_code": None}]}),
        encoding="utf-8")
    (root / "capture.json").write_text(json.dumps({"config": {"min_candidate_query_sim": 0.88}, "funnel": {
        "documents_input": ["d1", "d2", "d3"], "near_dup_dropped": [], "relevance_dropped": ["d3"],
        "clusters_scored": [["d1", "d2"], ["d3"]], "clusters_selected": [["d1", "d2"]], "max_candidates": 1}}), encoding="utf-8")
    (root / "analysis.json").write_text(json.dumps({"analysis_id": "a", "candidates": [
        {"candidate_id": "k1", "cluster_index": 0, "title_auto": "agent payments", "keyphrases": ["payments"],
         "decision_reason": "MODEL_SCORE", "document_ids": ["d1", "d2"]},
        {"candidate_id": "k2", "cluster_index": 1, "title_auto": "market", "keyphrases": [],
         "decision_reason": "LOW_QUERY_RELEVANCE", "document_ids": ["d2"]}]}), encoding="utf-8")
    (root / "trace.json").write_text(json.dumps({"narrate": {
        "path": "rubric", "config": {"rubric_pool": 1}, "candidates": {"weak": ["k1"], "excluded": ["k2"]}, "pool": ["k1"],
        "rest": [["k2", "OFF_TOPIC", "LOW_QUERY_RELEVANCE"]], "judged": {"k1": {"code": "R", "technology_ru": "Агентские платежи"}},
        "accepted": ["k1"], "uncertain_fill": [], "rounds": [{"finalized": ["k1"]}], "shown": [[1, "k1", 0.9, "R", True]]}}),
        encoding="utf-8")
    (root / "job.json").write_text(json.dumps({"items": [{"item_id": "i1", "rank": 1, "candidate_id": "k1",
        "title_ru": "Агентские платежи (agent payments)", "description_ru": "", "score": 0.9, "narrative_status": "GENERATED"}],
        "sources": [{"item_id": "i1", "document_id": "d1", "position": 1}]}, ensure_ascii=False), encoding="utf-8")
    write_manifest(root, {"job_id": "j", "topic": topic})
    return root


class FunnelTest(unittest.TestCase):
    def test_funnel_counts_cuts_and_source_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = funnel.funnel(load_snapshot(_snapshot(Path(tmp))))
        self.assertEqual(report["collection"]["not_polled"], ["github"])
        self.assertEqual(report["collection"]["empty"], ["zenodo"])
        self.assertEqual(report["collection"]["sources"]["arxiv"]["success_share"], 1.0)
        self.assertEqual(report["collection"]["events"], {"ACCEPTED": 1, "DEDUP": 1, "REJECTED": 1})
        self.assertEqual(report["analysis"]["clusters_cut_by_max_candidates"], 1)
        self.assertEqual(report["analysis"]["below_candidate_query_sim"], 1)
        self.assertEqual(report["selection"]["cut_by_pool_low_query_relevance"], 1)
        self.assertIn("| перспективные решения в финтехе |", funnel.render_markdown([report]))


class ExpertMatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            {"row_id": "pos-1", "label": 1, "domain_tag": "fintech", "title": "Агентские платежи (agent payments)",
             "source_urls": ["https://arxiv.org/pdf/2511.15712"]},
            {"row_id": "pos-2", "label": 1, "domain_tag": "fintech", "title": "Квантовая криптография в банках", "source_urls": []},
            {"row_id": "pos-3", "label": 1, "domain_tag": "fintech", "title": "Токенизация депозитов", "source_urls": []},
            {"row_id": "neg-1", "label": 0, "domain_tag": "fintech", "title": "Мобильный банк"},
        ]

    def test_keys_and_terms(self) -> None:
        self.assertIn("arxiv:2511.15712", expert_match.url_keys("https://arxiv.org/abs/2511.15712v1"))
        self.assertIn("arxiv:2511.15712", expert_match.url_keys("https://doi.org/10.48550/arxiv.2511.15712"))
        self.assertIn("doi:10.1038/nature14539", expert_match.url_keys("https://doi.org/10.1038/nature14539"))
        terms = dict(expert_match.expert_terms(self.rows[0], {"pos-1": ["агентские транзакции", "ИИ"]}))
        self.assertEqual(terms["agent payments"], "term")
        self.assertEqual(terms["агентские транзакции"], "synonym")
        self.assertNotIn("ии", terms)  # короче MIN_TERM_CHARS

    def test_match_review_and_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap = load_snapshot(_snapshot(Path(tmp) / "s" if (Path(tmp) / "s").mkdir() is None else Path(tmp)))
            result = expert_match.match([snap], self.rows, {"pos-3": ["обзор рынка"]})
            run = result["runs"][0]
            first = next(e for e in run["experts"] if e["row_id"] == "pos-1")
            self.assertEqual(first["stages"]["collected"][0]["rule"], "link")
            self.assertTrue(first["stages"]["shown"])
            out = Path(tmp) / "match"
            out.mkdir()
            (out / "matches.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            pairs = expert_match.review_pairs(result)
            self.assertTrue(pairs)
            self.assertTrue(all(p["rule"] != "link" for p in pairs))
            expert_match.write_sheets(pairs, out)
            for name, decision in (("reviewer_1.csv", "Y"), ("reviewer_2.csv", "N")):
                path = out / name
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
                for row in rows:
                    row["decision"] = decision
                with path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=expert_match.SHEET_FIELDS)
                    writer.writeheader()
                    writer.writerows(rows)
            report = expert_match.score(out)
        self.assertEqual(report["unresolved_pairs"], len(pairs))  # разногласия не засчитаны
        recall = report["runs"][0]["recall"]
        self.assertEqual(recall["collected"]["of"], 3)  # знаменатель — экспертные сигналы темы
        self.assertEqual(recall["collected"]["hits"], 1)  # только совпадение по ссылке
        self.assertEqual(report["runs"][0]["first_loss"]["pos-1"], "дошёл до выдачи")

    def test_topic_outside_case_is_marked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = expert_match.match([load_snapshot(_snapshot(Path(tmp), topic="облачные вычисления"))], self.rows, {})
        self.assertIsNone(result["runs"][0]["domain"])


class StatsTest(unittest.TestCase):
    def test_bootstrap_and_kappa(self) -> None:
        self.assertIsNone(stats.bootstrap_ci([0.5]))
        low, high = stats.bootstrap_ci([0.2, 0.4, 0.6, 0.8])
        self.assertLessEqual(low, 0.5)
        self.assertGreaterEqual(high, 0.5)
        self.assertEqual(stats.cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]), 1.0)
        self.assertIsNone(stats.cohen_kappa([1, 1], [1, 1]))  # согласие не отличимо от случайного
        diff = stats.paired_bootstrap({"a": 0.2, "b": 0.4, "c": 0.1}, {"a": 0.3, "b": 0.5, "c": 0.1, "d": 1.0})
        self.assertEqual(diff["topics"], 3)
        self.assertEqual(diff["only_in_b"], ["d"])


class CaptureTest(unittest.TestCase):
    def test_pack_roundtrip_keeps_dtypes_and_order(self) -> None:
        capture = AnalysisCapture(document_ids=["a", "b"], document_keys=["ka", "kb"],
                                  document_vectors=np.arange(6, dtype=np.float32).reshape(2, 3))
        capture.record_call("query: ", ["x", "y"], np.ones((2, 3), dtype=np.float32))
        capture.record_call("passage: ", ["z"], np.full((1, 3), 0.1, dtype=np.float64))
        documents, calls = unpack_vectors(pack_vectors(capture))
        self.assertEqual(documents["b"][0], "kb")
        self.assertTrue(np.array_equal(documents["b"][1], np.array([3, 4, 5], dtype=np.float32)))
        self.assertEqual([(c.prefix, c.texts, c.vectors.dtype.name) for c in calls],
                         [("query: ", ("x", "y"), "float32"), ("passage: ", ("z",), "float64")])

    def test_capturing_embedder_records_only_own_thread(self) -> None:
        class _Inner:
            model_name, dims = "m", 2

            def encode(self, texts, prefix):  # noqa: ANN001, ANN201
                return np.ones((len(texts), 2), dtype=np.float32)

        local = threading.local()
        embedder = CapturingEmbedder(_Inner(), local)
        local.capture = AnalysisCapture()
        other: list = []
        thread = threading.Thread(target=lambda: other.append(embedder.encode(["t"], "p")))
        thread.start()
        thread.join()
        embedder.encode(["own"], "q")
        self.assertEqual([c.texts for c in local.capture.text_calls], [("own",)])


class FindSnapshotsTest(unittest.TestCase):
    def test_model_manifest_inside_snapshot_is_not_a_snapshot(self) -> None:
        from ml.benchmark.snapshot import find_snapshots  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            root = _snapshot(Path(tmp) / "run" / "topic-1" if (Path(tmp) / "run" / "topic-1").mkdir(parents=True) is None else Path(tmp))
            model = root / "model" / "wsclf-2026.09.25-1"
            model.mkdir(parents=True)
            (model / "manifest.json").write_text(json.dumps({"model_version_id": "wsclf-2026.09.25-1"}), encoding="utf-8")
            found = find_snapshots([str(Path(tmp) / "run")])
        self.assertEqual([s.name for s in found], ["topic-1"])


class FreezeToolTest(unittest.TestCase):
    def test_env_secrets_are_masked_and_manifest_is_compatible(self) -> None:
        freeze = _freeze_module()
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("WS_API_KEY=abc\nWS_GIGACHAT_CREDENTIALS=zzz\nWS_RUBRIC_POOL=80\n# комментарий\n", encoding="utf-8")
            values = freeze.env_without_secrets(env)
            self.assertEqual(values, {"WS_API_KEY": "***", "WS_GIGACHAT_CREDENTIALS": "***", "WS_RUBRIC_POOL": "80"})
            root = Path(tmp) / "snap"
            root.mkdir()
            (root / "trace.json").write_text("{}", encoding="utf-8")
            freeze.write_manifest(root, {"job_id": "j", "topic": "t"})
            self.assertEqual(load_snapshot(root).verify(), [])
            self.assertEqual(freeze.slug("Перспективные решения в финтехе"), "perspektivnye-resheniya-v-fintekhe")


if __name__ == "__main__":
    unittest.main()
