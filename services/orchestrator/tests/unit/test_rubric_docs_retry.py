"""R-027 (WS_RUBRIC_DOCS_RETRY): документы пула rubric — пачки ≤ 50 с одним повтором; сбой загрузки отличается от
«нет документов» (RUBRIC_DOCS_UNAVAILABLE, id в трассе); полный сбой — отказ стадии с повтором задания, а не прежняя
ветка с карточками без источников."""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from orchestrator.application.dto import CandidateView, DocumentView, EvidenceView
from orchestrator.application.stages.analyze import AnalyzeConfig
from orchestrator.application.stages.collect import CollectConfig
from orchestrator.application.stages.expand import Glossary
from orchestrator.application.stages.narrate import NarrateConfig, run_narrate
from orchestrator.application.use_cases.run_job import RunJob, RunJobConfig
from orchestrator.domain.entities import Job, Query
from orchestrator.domain.errors import StageFailed, UpstreamUnavailable
from orchestrator.domain.values import Decision, JobStatus, TrustLevel

from ..fakes import (NOW, FakeAnalyzer, FakeClock, FakeCollector, FakeInsight, InMemoryJobRepository,
                     InMemoryResultRepository, make_candidate, new_id)

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
EARLY = "Первые пилотные партии в 2026 году, стартапы привлекли раунды, серийного производства нет."
R = dict(code="R", stage=2, trend=2, confidence=1.0, technology_ru="Фотонный лидар", profile_ru=EARLY,
         on_topic=True, concrete=True, early_stage=True, verifiable=True)
GENERIC = dict(code="N-GEN", stage=1, trend=1, confidence=0.9)


def doc(index: int) -> DocumentView:
    return DocumentView(document_id=new_id(6000 + index), title="Chip-scale photonic LiDAR",
                        url=f"https://www.site{index}.org/a", text="Первое предложение. Второе предложение.",
                        language_code="en", source_type="SCIENTIFIC_PUBLICATION", source_key="openalex",
                        trust_level=TrustLevel.HIGH, published_at=NOW)


def cand(index: int, docs: list[DocumentView], rank: int, decision: Decision = Decision.WEAK_SIGNAL,
         score: float = 0.9) -> CandidateView:
    evidence = tuple(EvidenceView(d.document_id, "Сниппет.", 0.9, d.source_type, d.trust_level) for d in docs)
    return replace(make_candidate(index, decision=decision, rank=rank, score=score), evidence=evidence,
                   decision_reason="MODEL_SCORE")


@dataclass
class FlakyCollector(FakeCollector):
    """collector со сбоями GetDocuments: первые fail_first вызовов и каждый вызов с id из fail_ids."""

    fail_first: int = 0
    fail_ids: set[str] = field(default_factory=set)
    transient: bool = True
    calls: list[list[str]] = field(default_factory=list)

    async def get_documents(self, document_ids: Sequence[str]) -> list[DocumentView]:
        self.calls.append(list(document_ids))
        if len(self.calls) <= self.fail_first or self.fail_ids.intersection(document_ids):
            raise UpstreamUnavailable("GetDocuments: UNAVAILABLE", transient=self.transient)
        return await super().get_documents(document_ids)


class RubricDocsRetryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.docs = [doc(i) for i in (1, 2, 3, 4)]
        self.good_a = cand(1, self.docs[:2], rank=1)
        self.good_b = cand(2, [self.docs[2]], rank=3, decision=Decision.INSUFFICIENT_EVIDENCE, score=0.1)
        self.good_c = cand(3, [self.docs[3]], rank=2)
        self.analyzer = FakeAnalyzer(candidates=[self.good_a, self.good_b, self.good_c])
        self.verdicts = {c.candidate_id: R for c in (self.good_a, self.good_b, self.good_c)}
        self.insight = FakeInsight(rubric_rule=lambda c: self.verdicts.get(c.candidate_id),
                                   finalize_rule=lambda c: {"title_ru": "Фотонный лидар"})
        self.results = InMemoryResultRepository()
        self.collector = self.flaky()

    def flaky(self, **failures: object) -> FlakyCollector:
        return FlakyCollector(documents={d.document_id: d for d in self.docs}, **failures)

    async def narrate(self, **overrides):  # noqa: ANN201
        async def check() -> None:
            return None
        config = replace(NarrateConfig(top_n=15, selection_mode="rubric",
                                       rubric_model_path=str(CONFIG_DIR / "rubric_ranker.json"),
                                       signal_model_path=str(CONFIG_DIR / "signal_classifier.json")), **overrides)
        return await run_narrate(analyzer=self.analyzer, collector=self.collector, insight=self.insight,
                                 results=self.results, job_id="job-1", query_text="твердотельные лидары",
                                 analysis_id=self.analyzer.analysis_id, config=config, check=check)

    def trace(self) -> dict:
        return self.results.traces["job-1"]["narrate"]

    def prefiltered(self) -> dict[str, str]:
        return {cid: reason for cid, _, reason in self.trace().get("prefiltered", [])}

    def excluded(self) -> dict[str, str]:
        return {e.candidate_id: e.decision_reason for e in self.results.excluded.get("job-1", [])}

    def shown(self) -> set[str]:
        return {i.candidate_id for i in self.results.items.get("job-1", [])}

    def add_plain(self, count: int) -> list[CandidateView]:
        """Кандидаты с одним документом: пул больше одной пачки из 50 id."""
        extra = []
        for n in range(count):
            document = doc(100 + n)
            self.collector.documents[document.document_id] = document
            extra.append(cand(100 + n, [document], rank=10 + n))
            self.verdicts[extra[-1].candidate_id] = GENERIC
        self.analyzer.candidates.extend(extra)
        return extra

    async def test_failed_batch_is_retried_and_pool_is_judged(self) -> None:
        self.collector = self.flaky(fail_first=1)  # первый вызов GetDocuments падает, повтор отвечает
        await self.narrate(rubric_docs_retry=True)
        self.assertNotIn("RUBRIC_NO_SOURCES", set(self.prefiltered().values()) | set(self.excluded().values()))
        self.assertEqual(self.trace()["path"], "rubric")
        self.assertEqual(self.shown(), {self.good_a.candidate_id, self.good_b.candidate_id, self.good_c.candidate_id})
        self.assertEqual(len(self.collector.calls), 2)
        self.assertEqual(self.collector.calls[0], self.collector.calls[1])  # повтор той же пачки
        self.assertEqual(self.trace()["documents_unavailable"], [])

    async def test_persistent_batch_failure_is_not_reported_as_no_sources(self) -> None:
        plain = self.add_plain(60)
        ghost = cand(4, [doc(9)], rank=3)  # документа нет у collector: «нет источников», а не сбой
        self.analyzer.candidates.append(ghost)
        self.collector.fail_ids = {self.docs[2].document_id}  # пачка с документом good_b падает и при повторе
        await self.narrate(rubric_docs_retry=True)
        self.assertEqual(self.prefiltered()[self.good_b.candidate_id], "RUBRIC_DOCS_UNAVAILABLE")
        lost = self.collector.calls[-1]
        self.assertEqual(self.trace()["documents_unavailable"], lost)
        unavailable = {c.candidate_id for c in (self.good_b, *plain) if c.evidence[0].document_id in lost}
        self.assertEqual({cid for cid, r in self.excluded().items() if r == "RUBRIC_DOCS_UNAVAILABLE"}, unavailable)
        self.assertEqual(len(unavailable), 15)
        self.assertEqual(self.excluded()[ghost.candidate_id], "RUBRIC_NO_SOURCES")
        self.assertEqual([len(c) for c in self.collector.calls], [50, 15, 15])
        self.assertEqual(self.collector.calls[1], lost)
        self.assertLessEqual({self.good_a.candidate_id, self.good_c.candidate_id}, self.shown())

    async def test_total_failure_fails_stage_instead_of_cards_without_sources(self) -> None:
        self.collector = self.flaky(fail_documents=True)
        with self.assertRaises(StageFailed) as raised:
            await self.narrate(rubric_docs_retry=True)
        self.assertEqual((raised.exception.error_code, raised.exception.retryable), ("UPSTREAM_UNAVAILABLE", True))
        self.assertEqual(self.results.items.get("job-1", []), [])  # прежняя ветка не строит карточки без источников
        self.assertEqual(self.results.excluded.get("job-1", []), [])  # повтор задания запишет настоящие причины
        self.assertEqual(self.insight.rubric_calls, [])
        trace = self.trace()
        self.assertEqual((trace["path"], trace["reason"]), ("stage_failed", "documents_unavailable"))
        self.assertEqual(trace["documents_unavailable"], self.collector.calls[0])
        self.assertEqual(len(trace["documents_unavailable"]), 4)
        self.assertEqual(set(self.prefiltered().values()), {"RUBRIC_DOCS_UNAVAILABLE"})
        self.assertEqual(len(self.collector.calls), 2)

    async def test_service_error_fails_stage_without_job_retry(self) -> None:
        self.collector = self.flaky(fail_first=99, transient=False)
        with self.assertRaises(StageFailed) as raised:
            await self.narrate(rubric_docs_retry=True)
        self.assertEqual((raised.exception.error_code, raised.exception.retryable), ("UPSTREAM_UNAVAILABLE", False))

    async def test_flag_off_failure_is_no_sources_and_legacy_as_before(self) -> None:
        self.collector = self.flaky(fail_first=1)
        await self.narrate()
        self.assertEqual(set(self.prefiltered().values()), {"RUBRIC_NO_SOURCES"})
        trace = self.trace()
        self.assertEqual((trace["path"], trace["reason"]), ("legacy", "rubric_unavailable"))
        self.assertNotIn("documents_unavailable", trace)
        d1, d2, d3, d4 = (document.document_id for document in self.docs)
        # одна пачка без повтора, дальше прежняя ветка грузит документы слабых сигналов по одному кандидату
        self.assertEqual(self.collector.calls, [[d1, d2, d4, d3], [d1, d2], [d4]])

    async def test_flag_off_total_failure_goes_to_legacy_without_sources_as_before(self) -> None:
        self.collector = self.flaky(fail_documents=True)
        outcome = await self.narrate()
        self.assertEqual(self.trace()["path"], "legacy")
        self.assertEqual(outcome.items_written, 2)
        self.assertTrue(all(not item.sources for item in self.results.items["job-1"]))

    async def test_batches_are_capped_at_50_only_with_flag(self) -> None:
        self.add_plain(60)
        await self.narrate()
        self.assertEqual([len(c) for c in self.collector.calls], [64])  # прежняя пачка до 150 id
        self.collector.calls.clear()
        self.results = InMemoryResultRepository()
        await self.narrate(rubric_docs_retry=True)
        self.assertEqual([len(c) for c in self.collector.calls], [50, 14])


class RunJobDocsUnavailableTest(unittest.IsolatedAsyncioTestCase):
    """Полный сбой GetDocuments в задании rubric: флаг доходит до стадии через RunJobConfig."""

    async def execute(self, retry: bool) -> JobStatus:
        clock = FakeClock()
        self.jobs = InMemoryJobRepository(clock)
        self.results = InMemoryResultRepository(self.jobs)
        candidates = [make_candidate(1, rank=1), make_candidate(2, rank=2)]
        run_job = RunJob(
            jobs=self.jobs, results=self.results, collector=FakeCollector(statuses=["COMPLETED"], fail_documents=True),
            analyzer=FakeAnalyzer(statuses=["COMPLETED"], candidates=candidates),
            insight=FakeInsight(), glossary=Glossary({"технологии": "technologies"}), clock=clock,
            config=RunJobConfig(heartbeat_seconds=0, collect=CollectConfig(poll_seconds=1.0),
                                analyze=AnalyzeConfig(poll_seconds=1.0),
                                narrate=NarrateConfig(selection_mode="rubric", rubric_docs_retry=retry)))
        query = Query.create(new_id(2), "слабые сигналы в ИИ", 2)
        self.job = await self.jobs.insert(query, Job(job_id=new_id(1), query_id=query.query_id), "key-0001:submit",
                                          "hash", clock.now())
        return await run_job.execute(self.job, "worker-1")

    async def test_flag_postpones_job_instead_of_cards_without_sources(self) -> None:
        self.assertIs(await self.execute(retry=True), JobStatus.QUEUED)
        self.assertEqual(self.jobs.postponements[self.job.job_id], 1)
        self.assertEqual(self.jobs.events[-1][3], "POSTPONED:UPSTREAM_UNAVAILABLE")
        self.assertNotIn(self.job.job_id, self.results.items)
        self.assertEqual(self.results.traces[self.job.job_id]["narrate"]["path"], "stage_failed")

    async def test_flag_off_job_finishes_with_cards_without_sources_as_before(self) -> None:
        self.assertIs(await self.execute(retry=False), JobStatus.PARTIAL)
        items = self.results.items[self.job.job_id]
        self.assertTrue(items and all(not item.sources for item in items))


if __name__ == "__main__":
    unittest.main()
