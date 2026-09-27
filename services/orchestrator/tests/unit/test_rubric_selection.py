"""Режим отбора rubric: состав источников и предфильтр по ТЗ, пул без порога модели, балл «стадия + тренд»,
пакетная доводка и откат к прежней стадии, если рубричная оценка недоступна."""

from __future__ import annotations

import unittest
from pathlib import Path
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


class CalibrationTest(unittest.TestCase):
    def test_parse_calibration(self) -> None:
        self.assertEqual(rubric.parse_calibration("1:2,2:3,3:4,4:4", 1, 4), {1: 2, 2: 3, 3: 4, 4: 4})
        self.assertEqual(rubric.parse_calibration("1:2,9:1,2:7", 1, 3), {1: 2, 2: 3, 3: 3})

    def test_market_mentions_in_composition(self) -> None:
        comp = rubric.composition_of([doc(1, "INDUSTRY_MEDIA", "Startup raises $20M for pilot"), doc(2)])
        self.assertEqual(comp.market_mentions, 1)
        self.assertIn("рыночными фактами", rubric.composition_ru(comp))


MODEL = str(Path(__file__).resolve().parents[2] / "config" / "rubric_ranker.json")
CLASSIFIER = str(Path(__file__).resolve().parents[2] / "config" / "signal_classifier.json")
EARLY = "Первые пилотные партии в 2026 году, стартапы привлекли раунды, серийного производства нет."
MATURE = "Массовое внедрение, отраслевой стандарт, рынок сформирован, выраженные лидеры."


class RubricNarrateFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.docs = {i: doc(i) for i in range(1, 9)}
        self.docs[5] = doc(5, "CODE_REPOSITORY")
        self.docs[4] = doc(4, title="A survey of sensing")
        self.docs[8] = doc(8, "INDUSTRY_MEDIA", "Startup raises seed round for photonic lidar", TrustLevel.MEDIUM)
        self.good_a = cand(1, [self.docs[1], self.docs[2]], rank=1)
        self.good_b = cand(2, [self.docs[3]], Decision.INSUFFICIENT_EVIDENCE, "MODEL_SCORE", score=0.1)
        self.overview = cand(3, [self.docs[4]], rank=2)
        self.github = cand(4, [self.docs[5]], rank=3)
        self.maybe = cand(5, [self.docs[6]], rank=4)
        self.market = cand(6, [self.docs[8]], Decision.INSUFFICIENT_EVIDENCE, "SINGLE_SOURCE", score=0.05)
        self.analyzer = FakeAnalyzer(candidates=[self.good_a, self.good_b, self.overview, self.github, self.maybe,
                                                 self.market])
        self.collector = FakeCollector(documents={d.document_id: d for d in self.docs.values()})
        self.results = InMemoryResultRepository()
        ok = dict(on_topic=True, concrete=True, early_stage=True, verifiable=True)
        self.verdicts = {
            self.good_a.candidate_id: dict(code="R", stage=2, trend=2, confidence=1.0, technology_ru="Фотонный лидар",
                                           profile_ru=EARLY, **ok),
            self.good_b.candidate_id: dict(code="R", stage=3, trend=3, confidence=1.0, technology_ru="Мемристорный чип",
                                           profile_ru=EARLY, **ok),
            self.market.candidate_id: dict(code="R", stage=2, trend=3, confidence=1.0, technology_ru="Лидар стартапа",
                                           profile_ru=EARLY, **ok),
            self.overview.candidate_id: dict(code="N-OVR", stage=1, trend=1, confidence=1.0,
                                             technology_ru="Обзор рынка лидаров", profile_ru=MATURE),
            self.maybe.candidate_id: dict(code="U", stage=1, trend=1, confidence=1.0, technology_ru="Лидары в авто",
                                          profile_ru=MATURE)}
        self.finalizable = {c.candidate_id for c in (self.good_a, self.good_b, self.overview, self.maybe, self.market)}
        self.insight = FakeInsight(rubric_rule=lambda c: self.verdicts.get(c.candidate_id),
                                   finalize_rule=lambda c: ({"title_ru": "Фотонный лидар", "companies": ["EPFL"]}
                                                            if c.candidate_id in self.finalizable else None))

    async def narrate(self, **overrides):  # noqa: ANN201
        async def check() -> None:
            return None
        config = replace(NarrateConfig(top_n=15, selection_mode="rubric", rubric_model_path=MODEL,
                                       signal_model_path=CLASSIFIER), **overrides)
        return await run_narrate(analyzer=self.analyzer, collector=self.collector, insight=self.insight,
                                 results=self.results, job_id="job-1", query_text="твердотельные лидары",
                                 analysis_id=self.analyzer.analysis_id, config=config, check=check)

    async def test_classifier_decides_threshold_filters_and_market_singleton_is_judged(self) -> None:
        outcome = await self.narrate()
        items = sorted(self.results.items["job-1"], key=lambda item: item.rank)
        shown = {i.candidate_id for i in items}
        self.assertEqual(shown, {self.good_a.candidate_id, self.good_b.candidate_id, self.market.candidate_id})
        self.assertTrue(all(0.5 <= i.score < 1.0 for i in items))
        self.assertTrue(items[0].features[0].feature_name.startswith("sc_"))
        self.assertIn("локальному классификатору", items[0].decision_explanation_ru)
        excluded = {e.candidate_id: e.decision_reason for e in self.results.excluded["job-1"]}
        self.assertEqual(excluded[self.overview.candidate_id], "RUBRIC_BELOW_TOP_N_N-OVR")
        self.assertEqual(excluded[self.github.candidate_id], "RUBRIC_NO_INDEPENDENT_SOURCE")
        self.assertEqual(self.insight.rubric_calls, [5])  # рыночная одиночная заметка вошла в пул рубрики
        self.assertEqual(outcome.weak_signals_total, 3)

    async def test_minimum_cards_when_nothing_passes_threshold(self) -> None:
        for fields in self.verdicts.values():
            fields["profile_ru"] = MATURE
        await self.narrate()
        self.assertEqual(len(self.results.items["job-1"]), 3)

    async def test_never_empty_when_finalize_fails(self) -> None:
        self.finalizable = set()
        outcome = await self.narrate()
        self.assertEqual(outcome.items_written, 3)  # прежний генератор карточек вместо пустой выдачи
        self.assertTrue(all(i.title_ru.endswith("(нарратив)") for i in self.results.items["job-1"]))

    async def test_judge_unavailable_falls_back_to_legacy(self) -> None:
        self.insight.rubric_rule = None
        outcome = await self.narrate()
        self.assertEqual(outcome.items_written, 4)
        self.assertFalse(any(i.decision_reason == "RUBRIC_MODEL" for i in self.results.items["job-1"]))

    async def test_rubric_r_is_shown_even_with_low_model_probability(self) -> None:
        self.verdicts[self.good_a.candidate_id]["profile_ru"] = MATURE
        await self.narrate()
        shown = {i.candidate_id: i for i in self.results.items["job-1"]}
        self.assertIn(self.good_a.candidate_id, shown)  # 27.09 порог модели скрыл 15 таких кандидатов
        self.assertLess(shown[self.good_a.candidate_id].score, 0.5)
        ranks = {cid: item.rank for cid, item in shown.items()}
        self.assertGreater(ranks[self.good_a.candidate_id], ranks[self.good_b.candidate_id])

    async def test_untrusted_and_far_candidates_enter_rubric_pool(self) -> None:
        media = doc(9, "INDUSTRY_MEDIA", "Startup pilots silicon photonics lidar", TrustLevel.MEDIUM)
        self.collector.documents[media.document_id] = media
        untrusted = cand(7, [media, self.docs[7]], Decision.INSUFFICIENT_EVIDENCE, "NO_TRUSTED_SOURCE", score=0.2)
        far = cand(8, [self.docs[2]], Decision.OFF_TOPIC, "LOW_QUERY_RELEVANCE", score=0.3)
        self.analyzer.candidates.extend([untrusted, far])
        await self.narrate()
        self.assertEqual(self.insight.rubric_calls, [7])  # 5 прежних + NO_TRUSTED_SOURCE + LOW_QUERY_RELEVANCE

    async def test_without_classifier_model_v3_is_used(self) -> None:
        await self.narrate(signal_model_path="", rubric_min_probability=0.0)
        items = self.results.items["job-1"]
        self.assertTrue(items and items[0].features[0].feature_name.startswith("lm_"))


if __name__ == "__main__":
    unittest.main()
