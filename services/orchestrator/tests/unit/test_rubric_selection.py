"""Режим отбора rubric: состав источников и предфильтр по ТЗ, пул без порога модели, балл «стадия + тренд»,
пакетная доводка и откат к прежней стадии, если рубричная оценка недоступна."""

from __future__ import annotations

import unittest
from dataclasses import replace

from orchestrator.application.dto import CandidateView, DocumentView, EvidenceView
from orchestrator.application.stages import rubric
from orchestrator.application.stages.narrate import NarrateConfig, run_narrate
from orchestrator.domain.values import Decision, TrustLevel

from ..fakes import NOW, FakeAnalyzer, FakeCollector, FakeInsight, InMemoryResultRepository, make_candidate, new_id


def doc(index: int, kind: str = "SCIENTIFIC_PUBLICATION", title: str = "Chip-scale photonic LiDAR",
        trust: TrustLevel = TrustLevel.HIGH) -> DocumentView:
    return DocumentView(document_id=new_id(5000 + index), title=title, url=f"https://www.site{index}.org/a",
                        text="Первое предложение. Второе предложение.", language_code="en", source_type=kind,
                        source_key="openalex", trust_level=trust, published_at=NOW)


def cand(index: int, docs: list[DocumentView], decision: Decision = Decision.WEAK_SIGNAL,
         reason: str = "MODEL_SCORE", rank: int = 1, score: float = 0.9) -> CandidateView:
    base = make_candidate(index, decision=decision, rank=rank, score=score)
    evidence = tuple(EvidenceView(d.document_id, "Сниппет.", 0.9, d.source_type, d.trust_level) for d in docs)
    return replace(base, evidence=evidence, decision_reason=reason)


class CompositionAndPrefilterTest(unittest.TestCase):
    def test_github_only_has_no_independent_source(self) -> None:
        comp = rubric.composition_of([doc(1, "CODE_REPOSITORY"), doc(2, "CODE_REPOSITORY")])
        decision, reason, text = rubric.prefilter(comp)
        self.assertEqual((decision, reason), (Decision.HYPE_OR_NOISE, "RUBRIC_NO_INDEPENDENT_SOURCE"))
        self.assertIn("ТЗ", text)

    def test_self_published_preprint_is_not_independent_but_arxiv_is(self) -> None:
        self.assertEqual(rubric.composition_of([doc(1, "PREPRINT", trust=TrustLevel.MEDIUM)]).independent, 0)
        self.assertEqual(rubric.composition_of([doc(1, "PREPRINT", trust=TrustLevel.HIGH)]).independent, 1)

    def test_review_dominated_is_excluded_and_mixed_passes(self) -> None:
        reviews = [doc(1, title="A review of LiDAR"), doc(2, title="Survey of sensing"), doc(3)]
        self.assertEqual(rubric.prefilter(rubric.composition_of(reviews))[1], "RUBRIC_REVIEWS")
        self.assertIsNone(rubric.prefilter(rubric.composition_of([doc(1), doc(2, "INDUSTRY_MEDIA", "Startup raises")])))

    def test_composition_text_has_years_and_market(self) -> None:
        text = rubric.composition_ru(rubric.composition_of([doc(1), doc(2, "INDUSTRY_MEDIA")]))
        self.assertIn(f"{NOW.year} — 2", text)
        self.assertIn("рыночных (СМИ, аналитика, пресс-релизы) 1", text)


class PoolAndRankTest(unittest.TestCase):
    def test_pool_keeps_model_score_exclusions_and_drops_rule_exclusions(self) -> None:
        weak = [cand(1, [doc(1)], rank=2), cand(2, [doc(2)], rank=1)]
        by_model = cand(3, [doc(3)], Decision.INSUFFICIENT_EVIDENCE, "MODEL_SCORE", score=0.2)
        by_rule = cand(4, [doc(4)], Decision.MATURE, "MATURITY_LEXICON")
        pool, rest = rubric.pool_candidates(weak, [by_model, by_rule], limit=40)
        self.assertEqual([c.candidate_id for c in pool], [weak[1].candidate_id, weak[0].candidate_id, by_model.candidate_id])
        self.assertEqual(rest, [by_rule])

    def test_rank_by_stage_plus_trend_then_confidence(self) -> None:
        from orchestrator.application.dto import JudgeVerdictView
        a, b, c = cand(1, [doc(1)]), cand(2, [doc(2)]), cand(3, [doc(3)])
        v = {a.candidate_id: JudgeVerdictView(a.candidate_id, "", 3, "", code="R", stage=2, trend=2, confidence=0.9),
             b.candidate_id: JudgeVerdictView(b.candidate_id, "", 3, "", code="R", stage=4, trend=3, confidence=0.5),
             c.candidate_id: JudgeVerdictView(c.candidate_id, "", 3, "", code="R", stage=3, trend=2, confidence=0.6)}
        self.assertEqual(rubric.rank_selected([a, b, c], v), [b, c, a])


class RubricNarrateFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.docs = {i: doc(i) for i in range(1, 7)}
        self.docs[5] = doc(5, "CODE_REPOSITORY")
        self.good_a = cand(1, [self.docs[1], self.docs[2]], rank=1)
        self.good_b = cand(2, [self.docs[3]], Decision.INSUFFICIENT_EVIDENCE, "MODEL_SCORE", score=0.1)
        self.overview = cand(3, [self.docs[4]], rank=2)
        self.github = cand(4, [self.docs[5]], rank=3)
        self.analyzer = FakeAnalyzer(candidates=[self.good_a, self.good_b, self.overview, self.github])
        self.collector = FakeCollector(documents={d.document_id: d for d in self.docs.values()})
        self.results = InMemoryResultRepository()
        verdicts = {self.good_a.candidate_id: dict(code="R", stage=2, trend=2, confidence=0.8, on_topic=True,
                                                   concrete=True, early_stage=True, verifiable=True),
                    self.good_b.candidate_id: dict(code="R", stage=3, trend=3, confidence=0.7, on_topic=True,
                                                   concrete=True, early_stage=True, verifiable=True),
                    self.overview.candidate_id: dict(code="N-OVR", stage=1, trend=1, confidence=0.9)}
        self.insight = FakeInsight(rubric_rule=lambda c: verdicts.get(c.candidate_id),
                                   finalize_rule=lambda c: ({"title_ru": "Фотонный лидар", "companies": ["EPFL"]}
                                                            if c.candidate_id == self.good_b.candidate_id else None))

    async def narrate(self, **overrides):  # noqa: ANN201
        async def check() -> None:
            return None
        config = replace(NarrateConfig(top_n=15, selection_mode="rubric"), **overrides)
        return await run_narrate(analyzer=self.analyzer, collector=self.collector, insight=self.insight,
                                 results=self.results, job_id="job-1", query_text="твердотельные лидары",
                                 analysis_id=self.analyzer.analysis_id, config=config, check=check)

    async def test_rubric_flow_orders_by_score_finalizes_and_explains_exclusions(self) -> None:
        outcome = await self.narrate()
        items = sorted(self.results.items["job-1"], key=lambda item: item.rank)
        self.assertEqual([i.candidate_id for i in items], [self.good_b.candidate_id, self.good_a.candidate_id])
        top = items[0]
        self.assertEqual((top.title_ru, top.predicted_stage, top.predicted_trend, top.score), ("Фотонный лидар", 3, 3, 0.7))
        self.assertIn("EPFL", top.explanation_ru)
        self.assertIn("балл 6 из 7", top.decision_explanation_ru)
        self.assertEqual(top.features[0].feature_name, "rubric_on_topic")
        self.assertEqual(items[1].title_ru, "Кандидат 1 (нарратив)")  # не прошла доводку → прежний генератор
        excluded = {e.candidate_id: e for e in self.results.excluded["job-1"]}
        self.assertEqual(excluded[self.overview.candidate_id].decision_reason, "RUBRIC_N-OVR")
        self.assertEqual(excluded[self.github.candidate_id].decision_reason, "RUBRIC_NO_INDEPENDENT_SOURCE")
        self.assertEqual((outcome.weak_signals_total, outcome.items_written), (2, 2))
        self.assertEqual(self.insight.rubric_calls, [3])

    async def test_without_rubric_verdicts_falls_back_to_legacy(self) -> None:
        self.insight.rubric_rule = None
        outcome = await self.narrate()
        self.assertEqual(outcome.items_written, 3)  # прежняя стадия: три кандидата WEAK_SIGNAL analyzer
        self.assertFalse(any(i.decision_reason == "RUBRIC_R" for i in self.results.items["job-1"]))

    async def test_top_n_overflow_is_explained(self) -> None:
        await self.narrate(top_n=1)
        excluded = {e.candidate_id: e.decision_reason for e in self.results.excluded["job-1"]}
        self.assertEqual(excluded[self.good_a.candidate_id], "RUBRIC_BELOW_TOP_N")


if __name__ == "__main__":
    unittest.main()
