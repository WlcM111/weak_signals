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

from ..fakes import (NOW, FakeAnalyzer, FakeClock, FakeCollector, FakeInsight, InMemoryResultRepository,
                     make_candidate, new_id)


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

    def test_model_mature_and_hype_leave_pool_only_when_flag_is_on(self) -> None:
        weak = [cand(1, [doc(1)], rank=1)]
        low = cand(2, [doc(2)], Decision.INSUFFICIENT_EVIDENCE, "MODEL_SCORE", score=0.3)
        mature = cand(3, [doc(3)], Decision.MATURE, "MODEL_SCORE", score=0.2)
        hype = cand(4, [doc(4)], Decision.HYPE_OR_NOISE, "MODEL_SCORE", score=0.1)
        pool, rest = rubric.pool_candidates(weak, [low, mature, hype], limit=40)
        self.assertIn(mature.candidate_id, [c.candidate_id for c in pool])  # прежнее поведение по умолчанию
        pool, rest = rubric.pool_candidates(weak, [low, mature, hype], limit=40, exclude_model_mature=True)
        self.assertEqual([c.candidate_id for c in pool], [weak[0].candidate_id, low.candidate_id])
        self.assertEqual({c.candidate_id for c in rest}, {mature.candidate_id, hype.candidate_id})

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
        # v14: единственный источник кандидата — обзор, правило обзоров отсекает его до рубрики
        self.assertEqual(excluded[self.overview.candidate_id], "RUBRIC_REVIEWS")
        self.assertEqual(excluded[self.github.candidate_id], "RUBRIC_NO_INDEPENDENT_SOURCE")
        self.assertEqual(self.insight.rubric_calls, [4])  # рыночная одиночная заметка вошла в пул рубрики
        self.assertEqual(outcome.weak_signals_total, 3)

    async def test_fill_takes_only_uncertain_never_rejected(self) -> None:
        for cid in (self.good_a.candidate_id, self.good_b.candidate_id, self.market.candidate_id):
            self.verdicts[cid]["code"] = "N-GEN"
        await self.narrate()
        shown = {i.candidate_id for i in self.results.items["job-1"]}
        self.assertEqual(shown, {self.maybe.candidate_id})  # только U; отвергнутые рубрикой не добираются

    async def test_confident_count_matches_displayed_confidence(self) -> None:
        outcome = await self.narrate(confidence_calibration=True)
        shown = [i for i in self.results.items["job-1"] if i.score >= 0.75]
        self.assertEqual(outcome.weak_signals_confident, len(shown))

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
        self.assertEqual(self.insight.rubric_calls, [6])  # 4 прежних + NO_TRUSTED_SOURCE + LOW_QUERY_RELEVANCE

    async def test_final_check_replaces_generic_card_and_calibrates_confidence(self) -> None:
        judged_once = set()
        base_rule = self.insight.rubric_rule

        def rule(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto == "Фотонный лидар" and candidate.candidate_id == self.good_b.candidate_id:
                if candidate.candidate_id in judged_once:
                    return None
                judged_once.add(candidate.candidate_id)
                return dict(code="N-GEN", stage=1, trend=1, confidence=0.9)
            return base_rule(candidate)
        self.insight.rubric_rule = rule
        await self.narrate(final_check_enabled=True, confidence_calibration=True)
        shown = {i.candidate_id: i for i in self.results.items["job-1"]}
        self.assertNotIn(self.good_b.candidate_id, shown)
        excluded = {e.candidate_id: e.decision_reason for e in self.results.excluded["job-1"]}
        self.assertEqual(excluded[self.good_b.candidate_id], "RUBRIC_FINAL_N-GEN")
        self.assertTrue(all(i.score >= 0.5 for i in shown.values()))  # принятые рубрикой — не ниже 50 %
        self.assertFalse(self.insight.rubric_modes[0])  # первая рубрика — обычный режим
        self.assertTrue(all(self.insight.rubric_modes[1:]) and len(self.insight.rubric_modes) > 1)  # финальная

    async def test_final_rejections_never_return_through_fallback(self) -> None:
        # аудит 29.09: когда финальная проверка отклоняла все карточки, резервная ветка показывала их снова
        base_rule = self.insight.rubric_rule

        def rule(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto == "Фотонный лидар":
                return dict(code="N-GEN", stage=1, trend=1, confidence=0.9)
            return base_rule(candidate)
        self.insight.rubric_rule = rule
        outcome = await self.narrate(final_check_enabled=True)
        self.assertEqual(self.results.items.get("job-1", []), [])
        self.assertEqual(outcome.items_written, 0)
        reasons = [e.decision_reason for e in self.results.excluded["job-1"]]
        self.assertEqual(reasons.count("RUBRIC_FINAL_N-GEN"), 3)

    # R-025: сбой финальной проверки или её пропуск не оставляют карточку принятой R
    UNCHECKED = "финальная проверка карточки не дала вердикта, требует экспертной проверки"

    def final_check_skips(self, skipped: set[str]) -> None:
        """Финальная проверка (название карточки после доводки) не возвращает вердикт для skipped."""
        base_rule = self.insight.rubric_rule

        def rule(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto == "Фотонный лидар" and candidate.candidate_id in skipped:
                return None
            return base_rule(candidate)
        self.insight.rubric_rule = rule

    async def test_final_check_failure_marks_cards_for_review_when_flag_on(self) -> None:
        self.insight.fail_final_check = True
        outcome = await self.narrate(final_check_enabled=True, confidence_calibration=True,
                                     final_check_fail_as_uncertain=True)
        shown = {i.candidate_id: i for i in self.results.items["job-1"]}
        self.assertEqual(set(shown), {self.good_a.candidate_id, self.good_b.candidate_id, self.market.candidate_id})
        for item in shown.values():  # как U: пометка в объяснении и уверенность ниже 50 %
            self.assertIn(self.UNCHECKED, item.decision_explanation_ru)
            self.assertLess(item.score, 0.5)
        self.assertEqual(outcome.weak_signals_confident, 0)
        narrate = self.results.traces["job-1"]["narrate"]
        self.assertEqual(narrate["final_unchecked"], sorted(shown))
        self.assertEqual({row[3] for row in narrate["shown"]}, {"U"})

    async def test_card_without_final_verdict_is_marked_and_checked_cards_stay(self) -> None:
        self.final_check_skips({self.good_b.candidate_id})
        await self.narrate(final_check_enabled=True, confidence_calibration=True, final_check_fail_as_uncertain=True)
        shown = {i.candidate_id: i for i in self.results.items["job-1"]}
        self.assertIn(self.UNCHECKED, shown[self.good_b.candidate_id].decision_explanation_ru)
        self.assertLess(shown[self.good_b.candidate_id].score, 0.5)
        for cid in (self.good_a.candidate_id, self.market.candidate_id):  # вердикт «прошла» — принятый R
            self.assertNotIn("требует экспертной проверки", shown[cid].decision_explanation_ru)
            self.assertGreaterEqual(shown[cid].score, 0.5)
        self.assertEqual(self.results.traces["job-1"]["narrate"]["final_unchecked"], [self.good_b.candidate_id])

    async def test_final_check_gaps_keep_cards_accepted_when_flag_off(self) -> None:
        self.final_check_skips({self.good_b.candidate_id})
        await self.narrate(final_check_enabled=True, confidence_calibration=True)
        card = {i.candidate_id: i for i in self.results.items["job-1"]}[self.good_b.candidate_id]
        self.assertNotIn("требует экспертной проверки", card.decision_explanation_ru)  # прежнее поведение
        self.assertGreaterEqual(card.score, 0.5)
        self.assertNotIn("final_unchecked", self.results.traces["job-1"]["narrate"])
        self.insight.fail_final_check = True
        self.results = InMemoryResultRepository()
        await self.narrate(final_check_enabled=True, confidence_calibration=True)
        items = self.results.items["job-1"]
        self.assertEqual(len(items), 3)
        self.assertTrue(all("требует" not in i.decision_explanation_ru and i.score >= 0.5 for i in items))

    async def test_fallback_cards_pass_final_check_when_flag_on(self) -> None:
        self.finalizable = set()  # доводка не вернула карточек — резервная ветка
        base_rule = self.insight.rubric_rule

        def rule(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto.endswith("(нарратив)"):  # финальная проверка резервной карточки
                if candidate.candidate_id == self.good_a.candidate_id:
                    return dict(code="N-MAT", stage=4, trend=1, confidence=0.9)
                if candidate.candidate_id == self.good_b.candidate_id:
                    return None
            return base_rule(candidate)
        self.insight.rubric_rule = rule
        outcome = await self.narrate(final_check_enabled=True, confidence_calibration=True,
                                     final_check_fail_as_uncertain=True)
        items = sorted(self.results.items["job-1"], key=lambda i: i.rank)
        self.assertEqual({i.candidate_id for i in items}, {self.good_b.candidate_id, self.market.candidate_id})
        self.assertEqual([i.rank for i in items], [1, 2])
        self.assertEqual(outcome.items_written, 2)
        by_id = {i.candidate_id: i for i in items}
        self.assertIn(self.UNCHECKED, by_id[self.good_b.candidate_id].decision_explanation_ru)
        self.assertNotIn("требует экспертной проверки", by_id[self.market.candidate_id].decision_explanation_ru)
        excluded = {e.candidate_id: e.decision_reason for e in self.results.excluded["job-1"]}
        self.assertEqual(excluded[self.good_a.candidate_id], "RUBRIC_FINAL_N-MAT")
        self.assertEqual(self.insight.rubric_modes, [False, True])
        self.assertTrue(self.results.traces["job-1"]["narrate"]["fallback"])

    async def test_fallback_without_time_for_final_check_is_marked(self) -> None:
        calls: list[int] = []

        def allowed() -> bool:
            calls.append(1)
            return len(calls) == 1  # срок кончился до первого раунда доводки

        await self.narrate(final_check_enabled=True, final_check_fail_as_uncertain=True, llm_allowed=allowed)
        items = self.results.items["job-1"]
        self.assertEqual(len(items), 3)
        self.assertTrue(all(self.UNCHECKED in i.decision_explanation_ru for i in items))
        self.assertNotIn(True, self.insight.rubric_modes)  # вызова LLM после срока нет

    async def test_fallback_cards_are_final_checked_when_flag_off(self) -> None:
        # этап 5.4: резервная ветка не обходит проверку зрелости; без вердикта карточка остаётся принятой, как раньше
        self.finalizable = set()
        base_rule = self.insight.rubric_rule

        def rule(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto.endswith("(нарратив)"):
                if candidate.candidate_id == self.good_a.candidate_id:
                    return dict(code="N-MAT", stage=4, trend=1, confidence=0.9)
                if candidate.candidate_id == self.good_b.candidate_id:
                    return None
            return base_rule(candidate)
        self.insight.rubric_rule = rule
        await self.narrate(final_check_enabled=True)
        items = {i.candidate_id: i for i in self.results.items["job-1"]}
        self.assertEqual(set(items), {self.good_b.candidate_id, self.market.candidate_id})
        self.assertNotIn("требует экспертной проверки", items[self.good_b.candidate_id].decision_explanation_ru)
        self.assertEqual(self.insight.rubric_modes, [False, True])
        narrate = self.results.traces["job-1"]["narrate"]
        self.assertEqual(narrate["final_missing"], [self.good_b.candidate_id])
        self.assertNotIn("final_unchecked", narrate)

    async def test_card_built_mostly_on_reviews_is_not_shown(self) -> None:
        # v12: карточке «AI-Driven Predictive Maintenance» доводка оставила из её источников три обзора
        review = doc(9, title="A review of predictive maintenance")
        self.collector.documents[review.document_id] = review
        mixed = cand(7, [self.docs[1], review, self.docs[2]], rank=5)  # 1 обзор из 3: предфильтр пропускает
        self.analyzer.candidates.append(mixed)
        self.verdicts[mixed.candidate_id] = dict(code="R", stage=2, trend=2, confidence=1.0,
                                                 technology_ru="Обслуживание", profile_ru=EARLY, on_topic=True,
                                                 concrete=True, early_stage=True, verifiable=True)
        base = self.insight.finalize_rule
        chosen = {"title_ru": "Предиктивное обслуживание", "source_ids": [review.document_id]}
        self.insight.finalize_rule = lambda c: chosen if c.candidate_id == mixed.candidate_id else base(c)
        await self.narrate()
        self.assertNotIn(mixed.candidate_id, {i.candidate_id for i in self.results.items["job-1"]})
        excluded = {e.candidate_id: e for e in self.results.excluded["job-1"]}
        self.assertEqual(excluded[mixed.candidate_id].decision_reason, "RUBRIC_REVIEWS")
        self.assertIn("источников карточки (1 из 1)", excluded[mixed.candidate_id].decision_explanation_ru)

    def add_plain_candidates(self, count: int) -> None:
        for n in range(count):
            document = doc(100 + n)
            self.collector.documents[document.document_id] = document
            extra = cand(100 + n, [document], rank=10 + n)
            self.analyzer.candidates.append(extra)
            self.verdicts[extra.candidate_id] = dict(code="N-GEN", stage=1, trend=1, confidence=0.9)

    async def test_large_pool_is_judged_in_chunks(self) -> None:
        self.add_plain_candidates(40)
        await self.narrate(rubric_pool=80)
        # 46 кандидатов в пуле: обзор и GitHub отсекает предфильтр, 44 оцениваются вызовами по 20
        self.assertEqual(self.insight.rubric_calls, [20, 20, 4])

    async def test_rubric_stops_between_chunks_when_deadline_is_near(self) -> None:
        self.add_plain_candidates(40)
        calls: list[int] = []

        def allowed() -> bool:
            calls.append(1)
            return len(calls) == 1  # хватает времени только на начало стадии и первую часть рубрики

        await self.narrate(rubric_pool=80, llm_allowed=allowed)
        self.assertEqual(self.insight.rubric_calls, [20])
        reasons = [e.decision_reason for e in self.results.excluded["job-1"]]
        self.assertEqual(reasons.count("RUBRIC_UNJUDGED"), 24)

    def test_display_score_is_monotone(self) -> None:
        from orchestrator.application.stages.narrate_rubric import display_score
        self.assertEqual(display_score(0.2, "R", True), 0.6)
        self.assertEqual(display_score(0.2, "U", True), 0.1)
        self.assertEqual(display_score(0.2, "R", False), 0.2)

    async def test_without_classifier_model_v3_is_used(self) -> None:
        await self.narrate(signal_model_path="", rubric_min_probability=0.0)
        items = self.results.items["job-1"]
        self.assertTrue(items and items[0].features[0].feature_name.startswith("lm_"))

    async def test_final_n_mat_removes_card_and_it_never_returns(self) -> None:
        # этап 5.4: зрелость, найденная финальной проверкой готовой карточки, убирает её из выдачи на штатном пути
        base_rule = self.insight.rubric_rule

        def rule(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto == "Фотонный лидар" and candidate.candidate_id == self.good_a.candidate_id:
                return dict(code="N-MAT", stage=1, trend=1, confidence=0.9)
            return base_rule(candidate)
        self.insight.rubric_rule = rule
        await self.narrate(final_check_enabled=True, confidence_calibration=True)
        shown = {i.candidate_id for i in self.results.items["job-1"]}
        self.assertNotIn(self.good_a.candidate_id, shown)
        self.assertIn(self.good_b.candidate_id, shown)
        excluded = {e.candidate_id: e.decision_reason for e in self.results.excluded["job-1"]}
        self.assertEqual(excluded[self.good_a.candidate_id], "RUBRIC_FINAL_N-MAT")
        narrate = self.results.traces["job-1"]["narrate"]
        self.assertEqual(narrate["final_dropped"][self.good_a.candidate_id]["code"], "N-MAT")
        self.assertEqual(narrate["final_missing"], [])

    async def test_final_n_mat_below_confidence_threshold_keeps_card(self) -> None:
        # этап 5.4: неуверенная зрелость не убирает сигнал; уверенная — убирает; порог 0 — прежнее поведение
        base_rule = self.insight.rubric_rule

        def rule(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto == "Фотонный лидар" and candidate.candidate_id == self.good_a.candidate_id:
                return dict(code="N-MAT", stage=1, trend=1, confidence=0.6)
            if candidate.title_auto == "Фотонный лидар" and candidate.candidate_id == self.good_b.candidate_id:
                return dict(code="N-MAT", stage=1, trend=1, confidence=0.8)
            return base_rule(candidate)
        self.insight.rubric_rule = rule
        await self.narrate(final_check_enabled=True, final_mature_min_confidence=0.7)
        shown = {i.candidate_id for i in self.results.items["job-1"]}
        self.assertIn(self.good_a.candidate_id, shown)
        self.assertNotIn(self.good_b.candidate_id, shown)
        narrate = self.results.traces["job-1"]["narrate"]
        self.assertEqual(narrate["final_mature_kept"], {self.good_a.candidate_id: "уверенность 0.60 < 0.70"})
        self.assertEqual(list(narrate["final_dropped"]), [self.good_b.candidate_id])
        self.results = InMemoryResultRepository()
        await self.narrate(final_check_enabled=True)
        shown = {i.candidate_id for i in self.results.items["job-1"]}
        self.assertFalse({self.good_a.candidate_id, self.good_b.candidate_id} & shown)

    async def test_final_n_mat_keeps_card_with_market_sources(self) -> None:
        # этап 5.4: рыночный факт (раунд, пилот, продукт) — признак ранней стадии; зрелость пары карточку не снимает
        base_rule = self.insight.rubric_rule

        def rule(candidate):  # noqa: ANN001, ANN202
            if candidate.title_auto == "Фотонный лидар" and candidate.candidate_id in (self.market.candidate_id,
                                                                                       self.good_b.candidate_id):
                return dict(code="N-MAT", stage=1, trend=1, confidence=0.9)
            return base_rule(candidate)
        self.insight.rubric_rule = rule
        await self.narrate(final_check_enabled=True)
        shown = {i.candidate_id for i in self.results.items["job-1"]}
        self.assertIn(self.market.candidate_id, shown)  # источник — отраслевое СМИ
        self.assertNotIn(self.good_b.candidate_id, shown)  # только научная публикация
        narrate = self.results.traces["job-1"]["narrate"]
        self.assertEqual(narrate["final_mature_kept"], {self.market.candidate_id: "рыночные источники"})
        self.results = InMemoryResultRepository()
        await self.narrate(final_check_enabled=True, final_mature_research_only=False)
        self.assertNotIn(self.market.candidate_id, {i.candidate_id for i in self.results.items["job-1"]})

    async def test_final_missing_is_traced_even_when_flag_off(self) -> None:
        # этап 5.4 (гипотеза H-FAILOPEN): карточки без вердикта финальной проверки видны в трассе; выдача прежняя
        self.final_check_skips({self.good_b.candidate_id})
        await self.narrate(final_check_enabled=True)
        narrate = self.results.traces["job-1"]["narrate"]
        self.assertEqual(narrate["final_missing"], [self.good_b.candidate_id])
        self.assertIn(self.good_b.candidate_id, {i.candidate_id for i in self.results.items["job-1"]})
        self.results = InMemoryResultRepository()
        await self.narrate()  # без финальной проверки поля нет
        self.assertNotIn("final_missing", self.results.traces["job-1"]["narrate"])


class AnalyzePoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_analyzer_gets_pool_size_as_max_candidates(self) -> None:
        from orchestrator.application.stages.analyze import AnalyzeConfig, run_analyze

        analyzer = FakeAnalyzer(statuses=["COMPLETED"])

        async def check() -> None:
            return None

        await run_analyze(analyzer, "job-1", new_id(1), "q", 15, AnalyzeConfig(max_candidates=80), FakeClock(), check)
        self.assertEqual(analyzer.max_candidates, [80])
        self.assertEqual(NarrateConfig().rubric_pool, 80)


if __name__ == "__main__":
    unittest.main()
