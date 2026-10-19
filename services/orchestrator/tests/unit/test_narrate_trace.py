"""R-003: трасса narrate (rubric) — документы с URL и итог каждого кандидата, прошлые прогоны стадии не стираются
(флаг WS_NARRATE_TRACE_EVIDENCE); при выключенном флаге трасса и отбор прежние."""

from __future__ import annotations

import unittest
from collections import Counter
from dataclasses import replace

from orchestrator.application.stages.narrate import NarrateConfig, run_narrate
from orchestrator.domain.values import Decision

from ..fakes import FakeAnalyzer, FakeCollector, FakeInsight, InMemoryResultRepository
from .test_rubric_selection import cand, doc

# Ключи трассы narrate до R-003.
BASELINE_KEYS = {
    "path", "config", "models", "candidates", "pool", "rest", "prefiltered", "judged", "raw_stage_trend", "unjudged",
    "probabilities", "accepted", "uncertain_fill", "below", "rounds", "deadline_cut", "not_tried", "text_failed",
    "review_dropped", "final_dropped", "fallback", "shown",
}
OK = dict(on_topic=True, concrete=True, early_stage=True, verifiable=True)


class NarrateTraceTest(unittest.IsolatedAsyncioTestCase):
    """Девять кандидатов — по одному на каждый исход стадии; без моделей вероятность задаёт уверенность рубрики."""

    def setUp(self) -> None:
        self.docs = {i: doc(i) for i in range(1, 14)}
        self.docs[8] = doc(8, "CODE_REPOSITORY")
        self.docs[9] = doc(9, title="A review of photonic sensing")
        d = self.docs
        self.shown = cand(1, [d[1], d[2]], rank=1)
        self.text_failed = cand(2, [d[3]], rank=2)
        self.final = cand(3, [d[4]], rank=3)
        self.reviews = cand(4, [d[5], d[9], d[6]], rank=4)
        self.not_tried = cand(5, [d[7]], rank=5)
        self.below = cand(6, [d[10]], Decision.INSUFFICIENT_EVIDENCE, "MODEL_SCORE", score=0.2)
        self.unjudged = cand(7, [d[11], d[12]], rank=6)  # документа 12 у collector нет
        self.github = cand(8, [d[8]], rank=7)
        self.rule = cand(9, [d[13], d[1]], Decision.MATURE, "MATURITY_LEXICON", score=0.3)  # вне пула
        self.analyzer = FakeAnalyzer(candidates=[self.shown, self.text_failed, self.final, self.reviews, self.not_tried,
                                                 self.below, self.unjudged, self.github, self.rule])
        self.collector = FakeCollector(documents={x.document_id: x for i, x in d.items() if i != 12})
        self.document_calls: list[list[str]] = []
        load = self.collector.get_documents

        async def counted(ids):  # noqa: ANN001, ANN202
            self.document_calls.append(list(ids))
            return await load(ids)

        self.collector.get_documents = counted
        self.verdicts = {c.candidate_id: dict(code="R", stage=2, trend=2, confidence=conf, technology_ru="Технология",
                                              profile_ru="Профиль", **OK)
                         for c, conf in ((self.shown, 1.0), (self.text_failed, 0.9), (self.final, 0.8),
                                         (self.reviews, 0.7), (self.not_tried, 0.6))}
        self.verdicts[self.below.candidate_id] = dict(code="U", stage=1, trend=1, confidence=1.0)
        self.finalizable = {self.shown.candidate_id, self.final.candidate_id, self.reviews.candidate_id}

        def rubric(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto == "Общее понятие":  # финальная проверка готовой карточки
                return dict(code="N-GEN", stage=1, trend=1, confidence=0.9)
            return self.verdicts.get(candidate.candidate_id)

        def finalize(candidate):  # noqa: ANN001, ANN202
            if candidate.candidate_id not in self.finalizable:
                return None
            if candidate.candidate_id == self.final.candidate_id:
                return {"title_ru": "Общее понятие"}
            if candidate.candidate_id == self.reviews.candidate_id:
                return {"title_ru": "Обзор", "source_ids": [self.docs[9].document_id]}
            return {"title_ru": f"Карточка {candidate.title_auto}"}

        self.insight = FakeInsight(rubric_rule=rubric, finalize_rule=finalize)
        self.results = InMemoryResultRepository()

    async def narrate(self, **overrides):  # noqa: ANN003, ANN201
        async def check() -> None:
            return None

        config = replace(NarrateConfig(top_n=2, selection_mode="rubric", final_check_enabled=True), **overrides)
        return await run_narrate(analyzer=self.analyzer, collector=self.collector, insight=self.insight,
                                 results=self.results, job_id="job-1", query_text="фотонные сенсоры",
                                 analysis_id=self.analyzer.analysis_id, config=config, check=check)

    def trace(self, stage: str = "narrate") -> dict:
        return self.results.traces["job-1"][stage]

    async def test_every_candidate_has_documents_and_one_outcome(self) -> None:
        result = await self.narrate(narrate_trace_evidence=True)
        trace = self.trace()
        ids = {c.candidate_id for c in self.analyzer.candidates}
        self.assertEqual(set(trace["evidence"]), ids)
        self.assertEqual(set(trace["outcome"]), ids)
        self.assertTrue(all(any(row[1] for row in rows) for rows in trace["evidence"].values()))  # ≥ 1 URL у каждого
        self.assertTrue(all(len(rows) <= 8 for rows in trace["evidence"].values()))
        stages = Counter(row[0] for row in trace["outcome"].values())
        self.assertEqual(sum(stages.values()), result.candidates_found)
        self.assertEqual({cid: row[0] for cid, row in trace["outcome"].items()}, {
            self.shown.candidate_id: "shown", self.text_failed.candidate_id: "text_failed",
            self.final.candidate_id: "final_check", self.reviews.candidate_id: "card_review",
            self.not_tried.candidate_id: "not_tried", self.below.candidate_id: "rubric",
            self.unjudged.candidate_id: "unjudged", self.github.candidate_id: "prefilter",
            self.rule.candidate_id: "not_in_pool"})
        # причина, решение и score — те же, что записаны в excluded; у показанного — ранг и уверенность карточки
        for row in self.results.excluded["job-1"]:
            self.assertEqual(trace["outcome"][row.candidate_id][1:], [row.decision_reason, row.decision.value,
                                                                       row.score, 0])
        item = self.results.items["job-1"][0]
        self.assertEqual(trace["outcome"][item.candidate_id], ["shown", "RUBRIC_MODEL", "WEAK_SIGNAL", item.score, 1])
        self.assertEqual(trace["outcome"][self.final.candidate_id][1], "RUBRIC_FINAL_N-GEN")
        # документы кандидата вне пула — одним дополнительным GetDocuments, только недостающие
        d13, d1 = self.docs[13], self.docs[1]
        self.assertEqual(self.document_calls[1:], [[d13.document_id]])
        self.assertEqual(trace["evidence"][self.rule.candidate_id], [
            [d13.document_id, d13.url, "SCIENTIFIC_PUBLICATION", "HIGH", True],
            [d1.document_id, d1.url, "SCIENTIFIC_PUBLICATION", "HIGH", True]])
        self.assertEqual(trace["evidence"][self.unjudged.candidate_id][1],
                         [self.docs[12].document_id, "", "SCIENTIFIC_PUBLICATION", "HIGH", False])

    async def test_flag_adds_only_trace_keys(self) -> None:
        await self.narrate()
        before = (self.trace(), [(e.candidate_id, e.decision, e.decision_reason, e.score)
                                 for e in self.results.excluded["job-1"]],
                  [(i.candidate_id, i.rank, i.score) for i in self.results.items["job-1"]])
        self.results = InMemoryResultRepository()
        await self.narrate(narrate_trace_evidence=True)
        trace = self.trace()
        self.assertEqual({k: v for k, v in trace.items() if k not in ("evidence", "outcome")}, before[0])
        self.assertEqual([(e.candidate_id, e.decision, e.decision_reason, e.score)
                          for e in self.results.excluded["job-1"]], before[1])
        self.assertEqual([(i.candidate_id, i.rank, i.score) for i in self.results.items["job-1"]], before[2])

    async def test_retry_keeps_previous_attempt(self) -> None:
        await self.narrate(narrate_trace_evidence=True)
        first = self.trace()
        self.finalizable.add(self.text_failed.candidate_id)  # повтор: доводка этой карточки удалась
        await self.narrate(narrate_trace_evidence=True)
        self.assertEqual(set(self.results.traces["job-1"]), {"narrate", "narrate@1"})
        self.assertIs(self.trace("narrate@1"), first)
        self.assertEqual(first["outcome"][self.text_failed.candidate_id][0], "text_failed")
        self.assertEqual(self.trace()["outcome"][self.text_failed.candidate_id][0], "shown")
        second = self.trace()
        await self.narrate(narrate_trace_evidence=True)
        self.assertEqual(set(self.results.traces["job-1"]), {"narrate", "narrate@1", "narrate@2"})
        self.assertIs(self.trace("narrate@2"), second)

    async def test_rubric_unavailable_trace_hands_all_to_legacy(self) -> None:
        self.insight.rubric_rule = None
        await self.narrate(narrate_trace_evidence=True)
        trace = self.trace()
        self.assertEqual((trace["path"], trace["reason"]), ("legacy", "rubric_unavailable"))
        self.assertEqual(trace["outcome"], {c.candidate_id: ["legacy", c.decision_reason, c.decision.value, c.score, 0]
                                            for c in self.analyzer.candidates})
        self.assertTrue(all(any(row[1] for row in rows) for rows in trace["evidence"].values()))
        self.assertIn("narrate_legacy", self.results.traces["job-1"])

    async def test_flag_off_trace_unchanged(self) -> None:
        await self.narrate()
        trace = self.trace()
        self.assertEqual(set(trace), BASELINE_KEYS)
        self.assertNotIn("narrate_trace_evidence", trace["config"])
        self.assertEqual(len(self.document_calls), 1)  # только документы пула
        await self.narrate()
        self.assertEqual(set(self.results.traces["job-1"]), {"narrate"})  # повтор перезаписывает, как раньше


if __name__ == "__main__":
    unittest.main()
