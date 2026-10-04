"""Режим А (этап 2, аудит A3): запись живого прогона → снимок → повтор без сети даёт ту же выдачу.

«Живой» прогон здесь — реальные сценарии analyzer, orchestrator и insight с включённой записью; внешние границы
заменены детерминированными тестовыми: эмбеддер по словам темы, Wikipedia по словарю, LLM по шаблону. Это проверка
полноты записи и точности повтора, а не обращения к реальным API (их проверяет повтор снимков стенда).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # фикстура модели analyzer: services/analyzer/tests/model_fixture.py

from analyzer.adapters.outbound.config_loader import load_feature_registry, load_lexicons  # noqa: E402
from analyzer.adapters.outbound.model_store import FileSystemModelStore  # noqa: E402
from analyzer.application.active_model import ActiveModelHolder  # noqa: E402
from analyzer.application.capture import pack_vectors  # noqa: E402
from analyzer.application.dto import EncyclopediaHit  # noqa: E402
from analyzer.application.use_cases.run_analysis import RunAnalysis, RunAnalysisConfig  # noqa: E402
from analyzer.domain.entities import Analysis  # noqa: E402
from analyzer.domain.values import AnalysisParams  # noqa: E402
from insight.adapters.outbound.llm.fake_provider import build_answer  # noqa: E402
from insight.application.dto import LLMResult  # noqa: E402
from insight.application.prompt_builder import PromptBuilder  # noqa: E402
from insight.application.provider_chain import ChainConfig, ProviderChain  # noqa: E402
from insight.application.use_cases.finalize_cards import FinalizeCards  # noqa: E402
from insight.application.use_cases.generate_insight import GenerateConfig, GenerateInsight  # noqa: E402
from insight.application.use_cases.judge_candidates import JudgeCandidates  # noqa: E402
from insight.application.use_cases.rubric_judge import RubricJudge  # noqa: E402
from ml.benchmark import replay as replay_module  # noqa: E402
from ml.benchmark.memory import (  # noqa: E402
    FrozenSyncClock,
    LoopbackAnalyzer,
    LoopbackCollector,
    LoopbackInsight,
    MemoryAnalysisRepository,
    MemoryCallLog,
    MemoryCandidateRepository,
    MemoryEmbeddingCache,
    MemoryInsightRepository,
    MemoryResultRepository,
    Misses,
    ReplayClock,
    SnapshotCollectorReader,
    feature_labels,
)
from ml.benchmark.snapshot import load_snapshot, write_manifest  # noqa: E402
from orchestrator.application.stages.narrate import NarrateConfig, run_narrate  # noqa: E402
from services.analyzer.tests.fakes import EMBEDDING_DIMS  # noqa: E402
from services.analyzer.tests.model_fixture import build_model_store  # noqa: E402
from ws_common.ids import set_correlation_id  # noqa: E402

AS_OF = datetime(2026, 10, 3, 12, tzinfo=UTC)
JOB_ID = "11111111-1111-4111-8111-111111111111"
ANALYSIS_ID = "22222222-2222-4222-8222-222222222222"
COLLECTION_ID = "33333333-3333-4333-8333-333333333333"
TOPIC = "квантовые сенсоры для навигации"
QUERY = f"{TOPIC} | quantum sensors | спутниковая связь | твердотельные аккумуляторы"
TOPICS = ("квантов", "спутник", "аккумулятор")


class TopicEmbedder:
    """Детерминированный эмбеддер: ось темы по слову, общий компонент и шум от хеша текста."""

    model_name = "intfloat/multilingual-e5-base"

    def __init__(self) -> None:
        self.dims = EMBEDDING_DIMS
        self.calls = 0

    def encode(self, texts, prefix):  # noqa: ANN001, ANN201
        self.calls += 1
        rows = []
        for text in texts:
            vector = np.zeros(self.dims, dtype=np.float32)
            vector[0] = 0.6
            for axis, word in enumerate(TOPICS, start=1):
                if word in text.lower():
                    vector[axis] += 1.0
            seed = int(hashlib.sha256((prefix + text).encode()).hexdigest()[:8], 16)
            vector += np.random.default_rng(seed).normal(0, 0.05, self.dims).astype(np.float32)
            rows.append(vector / np.linalg.norm(vector))
        return np.vstack(rows).astype(np.float32)


class LiveReader(SnapshotCollectorReader):
    """«Живой» collector: документы и ответы энциклопедии по словарю (одна статья существует)."""

    def check_encyclopedia(self, titles, language_code):  # noqa: ANN001, ANN201
        return [EncyclopediaHit(title=t, exists="спутник" in t.lower(), pageviews_30d=900 if "спутник" in t.lower() else -1)
                for t in titles]


class ScriptedLLM:
    """LLM теста: рубрика — R для двух из трёх кандидатов, доводка — пустой ответ, нарратив — из доказательств."""

    name = "gigachat"
    model = "GigaChat-2-Max"
    max_concurrency = 1

    async def complete(self, messages, *, json_schema, max_tokens, temperature, deadline_seconds):  # noqa: ANN001, ANN201
        schema = json.dumps(json_schema or {}, ensure_ascii=False)
        user = " ".join(m.content for m in messages if m.role == "user")
        if '"verdicts"' in schema:
            ids = list(dict.fromkeys(re.findall(r"\b(c\d{1,3})\b", user)))
            verdicts = [{"candidate_id": cid, "code": "R" if int(cid[1:]) % 3 else "N-GEN", "on_topic": True,
                         "concrete": True, "early_stage": True, "verifiable": True, "stage": 2, "trend": 2, "relevance": 2,
                         "confidence": 0.8, "reason_ru": "Ранние пилоты и прототипы в источниках.",
                         "technology_ru": f"Квантовый сенсор {cid}", "profile_ru": f"Прототипы квантовых сенсоров {cid}, пилоты"}
                        for cid in ids]
            text = json.dumps({"verdicts": verdicts}, ensure_ascii=False)
        elif '"cards"' in schema:
            text = json.dumps({"cards": []})
        else:
            text = build_answer(messages, json_schema)
        return LLMResult(text=text, model=self.model, prompt_tokens=len(user) // 4, completion_tokens=len(text) // 4,
                         latency_ms=7)

    async def healthcheck(self) -> bool:
        return True


class _Recorder:
    def __init__(self) -> None:
        self.entries: list = []

    async def record(self, entry) -> None:  # noqa: ANN001
        self.entries.append(entry)


class _Captures:
    def __init__(self, fail: bool = False) -> None:
        self.saved: dict = {}
        self.fail = fail

    def save(self, analysis_id, capture) -> None:  # noqa: ANN001
        if self.fail:
            raise RuntimeError("база недоступна")
        self.saved[analysis_id] = capture


def documents() -> list[dict]:
    rows, rank = [], 0
    plan = [("квантов", "Квантовый сенсор навигации"), ("спутник", "Спутниковая связь со смартфоном"),
            ("аккумулятор", "Твердотельный аккумулятор")]
    for word, title in plan:
        for index in range(5):
            rank += 1
            rows.append({"document_id": f"{rank:08d}-0000-4000-8000-000000000000", "relevance_rank": rank,
                         "matched_term": word, "url": f"https://example.org/{word}/{index}", "url_hash": f"h{rank}",
                         "canonical_url": f"https://example.org/{word}/{index}", "origin_domain": "example.org",
                         "title": f"{title}: прототип {index}",
                         "body_text": f"{title}. Первые пилоты и прототип {index}; {word} исследования 2026.",
                         "content_hash": f"c{rank}", "language_code": "ru", "published_at": "2026-05-01T00:00:00+00:00",
                         "source_key": "openalex", "source_type": "SCIENTIFIC_PUBLICATION", "trust_level": "HIGH",
                         "doi": None, "citation_count": index, "engagement_count": None, "raw_meta": {},
                         "fetched_at": "2026-10-03T11:00:00+00:00"})
    return rows


def analyze(root: Path) -> dict:
    """Только анализ без записи: кандидаты, строки документов и подписи признаков (для проверки пути gRPC)."""
    registry = load_feature_registry(ROOT / "schemas" / "feature_registry_v1.json")
    lexicons = load_lexicons(ROOT / "services/analyzer/config/lexicons", ROOT / "services/analyzer/config/stage_rules.yaml")
    build_model_store(root / "model", registry.model_names, dims=EMBEDDING_DIMS)
    bundle = FileSystemModelStore(root / "model", True).load_from_path("active", registry)
    rows = documents()
    candidates = MemoryCandidateRepository()
    analysis = Analysis(analysis_id=ANALYSIS_ID, idempotency_key=f"{JOB_ID}:analyze", collection_id=COLLECTION_ID,
                        query_text=QUERY, model_version_id=bundle.version.model_version_id,
                        params=AnalysisParams(top_n=5, max_candidates=5, weak_signal_threshold=0.01, min_evidence_documents=1))
    RunAnalysis(analyses=MemoryAnalysisRepository(), candidates=candidates, embedding_cache=MemoryEmbeddingCache({}),
                collector=LiveReader(rows, [], Misses()), embedder=TopicEmbedder(), active_model=ActiveModelHolder(bundle),
                lexicons=lexicons, clock=FrozenSyncClock(AS_OF),
                config=RunAnalysisConfig(min_doc_query_sim=0.25, min_cluster_size=2, query_relevance_mode="max")).execute(analysis, "live")
    candidates.assign_ids([], "live")
    return {"candidates": candidates, "rows": rows,
            "labels": feature_labels(registry, str(ROOT / "schemas" / "feature_registry_v1.json"))}


def live_run(root: Path, *, fail_capture: bool = False, record: bool = True) -> dict:
    """Прогон с записью; пишет снимок в `root` в формате tools/benchmark/freeze.py."""
    registry = load_feature_registry(ROOT / "schemas" / "feature_registry_v1.json")
    lexicons = load_lexicons(ROOT / "services/analyzer/config/lexicons", ROOT / "services/analyzer/config/stage_rules.yaml")
    model_root = root / "model"
    build_model_store(model_root, registry.model_names, dims=EMBEDDING_DIMS)
    bundle = FileSystemModelStore(model_root, True).load_from_path("active", registry)
    rows = documents()
    misses = Misses()
    captures = _Captures(fail=fail_capture) if record else None
    analyses, candidates = MemoryAnalysisRepository(), MemoryCandidateRepository()
    config = RunAnalysisConfig(min_doc_query_sim=0.25, min_cluster_size=2, query_relevance_mode="max")
    analysis = Analysis(analysis_id=ANALYSIS_ID, idempotency_key=f"{JOB_ID}:analyze", collection_id=COLLECTION_ID,
                        query_text=QUERY, model_version_id=bundle.version.model_version_id,
                        params=AnalysisParams(top_n=5, max_candidates=5, weak_signal_threshold=0.01, min_evidence_documents=1))
    status = RunAnalysis(analyses=analyses, candidates=candidates, embedding_cache=MemoryEmbeddingCache({}),
                         collector=LiveReader(rows, [], misses), embedder=TopicEmbedder(), active_model=ActiveModelHolder(bundle),
                         lexicons=lexicons, clock=FrozenSyncClock(AS_OF), config=config, captures=captures).execute(analysis, "live")
    if fail_capture:
        return {"status": status.value}
    candidates.assign_ids([], "live")
    capture = captures.saved[ANALYSIS_ID] if captures is not None else None
    set_correlation_id(JOB_ID)
    recorder = _Recorder()
    clock = ReplayClock(AS_OF)
    chain = ProviderChain([ScriptedLLM()], MemoryCallLog(), clock, ChainConfig(temperature=0.2),
                          recorder=recorder if record else None)
    prompts = PromptBuilder(ROOT / "services/insight/prompts", ROOT / "services/insight/schemas")
    insight = LoopbackInsight(JudgeCandidates(chain, prompts), RubricJudge(chain, prompts), FinalizeCards(chain, prompts),
                              GenerateInsight(chain, prompts, MemoryInsightRepository(), clock, GenerateConfig()))
    orchestrator_config = ROOT / "services/orchestrator/config"
    narrate_config = NarrateConfig(top_n=4, selection_mode="rubric", rubric_pool=5, final_check_enabled=True,
                                   confidence_calibration=True, signal_model_path=str(orchestrator_config / "signal_classifier.json"),
                                   rubric_model_path=str(orchestrator_config / "rubric_ranker.json"), profile_model_path="")
    results = MemoryResultRepository()

    async def check() -> None:
        return None

    asyncio.run(run_narrate(analyzer=LoopbackAnalyzer(candidates, feature_labels(registry, str(ROOT / "schemas" / "feature_registry_v1.json"))),
                            collector=LoopbackCollector(rows), insight=insight, results=results, job_id=JOB_ID, query_text=TOPIC,
                            analysis_id=ANALYSIS_ID, config=narrate_config, check=check))
    if not record:
        return {"status": status.value, "items": sorted(results.items, key=lambda i: i.rank)}
    (root / "documents.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (root / "capture.json").write_text(json.dumps({
        "as_of": capture.as_of.isoformat(), "model_version_id": capture.model_version_id,
        "embedding_model": capture.embedding_model, "query_text": capture.query_text, "params": capture.params,
        "config": capture.config, "funnel": capture.funnel, "encyclopedia": capture.encyclopedia}, ensure_ascii=False,
        default=str), encoding="utf-8")
    (root / "vectors.npz").write_bytes(pack_vectors(capture))
    (root / "analysis.json").write_text(json.dumps({"analysis_id": ANALYSIS_ID, "collection_id": COLLECTION_ID, "candidates": [
        {"candidate_id": c.candidate_id, "cluster_index": c.cluster_index, "title_auto": c.title_auto,
         "keyphrases": list(c.keyphrases), "decision": c.decision.value, "decision_reason": c.decision_reason.value,
         "score": c.score, "document_ids": [d.document_id for d in c.documents]} for c in candidates.candidates]},
        ensure_ascii=False), encoding="utf-8")
    (root / "trace.json").write_text(json.dumps({"job": {"query_text": TOPIC}, **results.traces}, ensure_ascii=False, default=str),
                                     encoding="utf-8")
    items = sorted(results.items, key=lambda i: i.rank)
    (root / "job.json").write_text(json.dumps({"job": {"job_id": JOB_ID, "query_text": TOPIC}, "items": [
        {"item_id": str(n), "rank": i.rank, "candidate_id": i.candidate_id, "title_ru": i.title_ru, "score": i.score,
         "narrative_status": i.narrative_status.value, "description_ru": i.description_ru} for n, i in enumerate(items)],
        "sources": [{"item_id": str(n), "document_id": s.document_id, "position": s.position}
                    for n, i in enumerate(items) for s in i.sources]}, ensure_ascii=False), encoding="utf-8")
    (root / "llm_recordings.jsonl").write_text("".join(json.dumps({
        **{k: v for k, v in dataclasses.asdict(e).items() if k != "messages"},
        "messages": [{"role": r, "content": c} for r, c in e.messages]}, ensure_ascii=False) + "\n"
        for e in recorder.entries), encoding="utf-8")
    shutil.copytree(ROOT / "services/insight/prompts", root / "insight/prompts")
    shutil.copytree(ROOT / "services/insight/schemas", root / "insight/schemas")
    (root / "orchestrator_config").mkdir()
    for name in ("signal_classifier.json", "rubric_ranker.json"):
        shutil.copy2(orchestrator_config / name, root / "orchestrator_config" / name)
    write_manifest(root, {"job_id": JOB_ID, "topic": TOPIC})
    return {"status": status.value, "items": items, "traces": results.traces, "recordings": recorder.entries,
            "capture": capture, "candidates": candidates.candidates}


class ReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "snap"
        cls.root.mkdir()
        cls.live = live_run(cls.root)
        cls.snapshot = load_snapshot(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_live_run_recorded_every_boundary(self) -> None:
        self.assertEqual(self.live["status"], "COMPLETED")
        capture = self.live["capture"]
        self.assertEqual(capture.as_of, AS_OF)
        self.assertTrue(capture.text_calls)
        self.assertEqual(len(capture.document_ids), len(documents()))
        self.assertTrue(capture.encyclopedia)
        for key in ("documents_input", "near_dup_dropped", "relevance", "relevance_dropped", "clusters_scored",
                    "clusters_selected", "small_cluster_documents", "max_candidates"):
            self.assertIn(key, capture.funnel)
        narrate = self.live["traces"]["narrate"]
        self.assertEqual(narrate["path"], "rubric")
        for key in ("pool", "rest", "judged", "probabilities", "accepted", "rounds", "text_failed", "shown", "config"):
            self.assertIn(key, narrate)
        self.assertTrue(narrate["judged"])
        self.assertTrue(self.live["items"])
        self.assertTrue(all(e.correlation_id == JOB_ID for e in self.live["recordings"]))

    def test_replay_reproduces_live_output_and_is_stable(self) -> None:
        first = replay_module.replay(self.snapshot)
        second = replay_module.replay(self.snapshot)
        self.assertEqual(first.issues, [])
        self.assertEqual(first.misses["llm"] + first.misses["embeddings"] + first.misses["encyclopedia"], 0)
        self.assertEqual(first.status, replay_module.IDENTICAL, first.differences)
        self.assertEqual(first.items, first.live_items)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(set(first.candidates.values()), {"live"})
        self.assertEqual(first.trace["narrate"]["judged"], self.live["traces"]["narrate"]["judged"])

    def test_ablation_with_new_llm_requests_is_marked_incomplete(self) -> None:
        # В живом прогоне три кандидата и пул 5: пул 2 меняет состав запроса рубрики — новый запрос без записи.
        result = replay_module.replay(self.snapshot, replay_module.parse_overrides(["narrate.rubric_pool=2"]))
        self.assertEqual(result.status, replay_module.INCOMPLETE)
        self.assertGreater(result.misses["llm"], 0)
        self.assertEqual(result.overrides, {"narrate.rubric_pool": 2})

    def test_override_without_new_requests_is_identical(self) -> None:
        # Пул больше числа кандидатов не меняет ни запросов, ни выдачи: повтор совпадает с живым прогоном.
        result = replay_module.replay(self.snapshot, replay_module.parse_overrides(["narrate.rubric_pool=50"]))
        self.assertEqual(result.status, replay_module.IDENTICAL, result.differences)

    def test_recording_does_not_change_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain = live_run(Path(tmp), record=False)
        self.assertEqual([(i.rank, i.candidate_id, i.title_ru, i.score) for i in plain["items"]],
                         [(i.rank, i.candidate_id, i.title_ru, i.score) for i in self.live["items"]])

    def test_corrupted_snapshot_is_reported(self) -> None:
        copy = Path(self._tmp.name) / "broken"
        shutil.copytree(self.root, copy)
        path = copy / "documents.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("прототип 0", "прототип X", 1), encoding="utf-8")
        result = replay_module.replay(load_snapshot(copy))
        self.assertIn("не совпала SHA-256 файла documents.jsonl", result.issues)

    def test_missing_file_stops_replay_explicitly(self) -> None:
        copy = Path(self._tmp.name) / "no-vectors"
        shutil.copytree(self.root, copy)
        (copy / "vectors.npz").unlink()
        result = replay_module.replay(load_snapshot(copy))
        self.assertEqual(result.status, replay_module.INCOMPLETE)
        self.assertTrue(any("vectors.npz" in issue for issue in result.issues))

    def test_bad_override_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            replay_module.parse_overrides(["min_candidate_query_sim=0.8"])

    def test_capture_failure_does_not_fail_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(live_run(Path(tmp), fail_capture=True)["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
