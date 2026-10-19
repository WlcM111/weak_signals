"""R-026: сбой RPC FinalizeCards не «сжигает» пакет лучших R (флаг WS_RUBRIC_FINALIZE_RETRY_SMALL).

Дублёр insight обрывает большой пакет (как срок RPC при многих последовательных вызовах LLM) и доводит малые;
как insight, за вызов доводит не больше 30 карточек. До правки и при выключенном флаге лучшие R уходят
в RUBRIC_TEXT_FAILED, а показываются следующие по вероятности.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field, replace

from orchestrator.application.dto import DocumentView, EvidenceView
from orchestrator.application.stages.narrate import NarrateConfig, run_narrate
from orchestrator.application.use_cases.run_job import _narrate_config
from orchestrator.domain.errors import UpstreamUnavailable
from orchestrator.domain.values import TrustLevel

from ..fakes import NOW, FakeAnalyzer, FakeCollector, FakeInsight, InMemoryResultRepository, make_candidate, new_id

INSIGHT_MAX_CARDS = 30  # insight: finalize_cards.MAX_CARDS


@dataclass
class FlakyFinalizeInsight(FakeInsight):
    """Сбой RPC: пакет больше `fail_above` карточек, любой пакет с кандидатом из `broken`, первый малый пакет
    с кандидатом из `flaky_once`. Доводит не больше 30 карточек вызова, как insight."""

    fail_above: int = 1000
    broken: set[str] = field(default_factory=set)
    flaky_once: set[str] = field(default_factory=set)
    sizes: list[int] = field(default_factory=list)

    async def finalize_cards(self, query_text: str, cards) -> dict:  # noqa: ANN001
        self.sizes.append(len(cards))
        ids = {card.candidate.candidate_id for card in cards}
        if len(cards) > self.fail_above or ids & self.broken:
            raise UpstreamUnavailable("FinalizeCards: DEADLINE_EXCEEDED")
        if ids & self.flaky_once:
            self.flaky_once -= ids
            raise UpstreamUnavailable("FinalizeCards: UNAVAILABLE")
        return await super().finalize_cards(query_text, list(cards)[:INSIGHT_MAX_CARDS])


class FinalizeRetrySmallTest(unittest.IsolatedAsyncioTestCase):
    def build(self, count: int, fail_above: int = 1000) -> None:
        documents: dict[str, DocumentView] = {}
        self.candidates = []
        for index in range(count):
            document = DocumentView(new_id(7000 + index), f"Method {index}", f"https://h{index}.org/a",
                                    "Первое предложение. Второе предложение.", "en", "SCIENTIFIC_PUBLICATION",
                                    "openalex", TrustLevel.HIGH, NOW)
            documents[document.document_id] = document
            evidence = (EvidenceView(document.document_id, "Сниппет.", 0.9, document.source_type,
                                     document.trust_level),)
            self.candidates.append(replace(make_candidate(index, rank=index + 1), evidence=evidence))
        # без моделей вероятность = 0,7 + 0,05 × уверенность рубрики: порядок по вероятности — порядок номеров
        confidence = {c.candidate_id: 1 - index / 100 for index, c in enumerate(self.candidates)}
        self.analyzer = FakeAnalyzer(candidates=list(self.candidates))
        self.collector = FakeCollector(documents=documents)
        self.results = InMemoryResultRepository()
        self.insight = FlakyFinalizeInsight(
            rubric_rule=lambda c: dict(code="R", stage=2, trend=2, confidence=confidence[c.candidate_id],
                                       on_topic=True, concrete=True, early_stage=True, verifiable=True,
                                       technology_ru=c.title_auto, profile_ru="Пилотные партии, серии нет."),
            finalize_rule=lambda c: {"title_ru": f"Карточка: {c.title_auto}"}, fail_above=fail_above)

    async def narrate(self, top_n: int, retry: bool, llm_allowed=None) -> None:  # noqa: ANN001
        async def check() -> None:
            return None
        config = NarrateConfig(top_n=top_n, selection_mode="rubric", rubric_pool=100, llm_allowed=llm_allowed)
        if retry:
            config = replace(config, rubric_finalize_retry_small=True)
        await run_narrate(analyzer=self.analyzer, collector=self.collector, insight=self.insight,
                          results=self.results, job_id="job-1", query_text="твердотельные лидары",
                          analysis_id=self.analyzer.analysis_id, config=config, check=check)

    def ids(self, start: int, stop: int) -> list[str]:
        return [c.candidate_id for c in self.candidates[start:stop]]

    def shown(self) -> list[str]:
        return [item.candidate_id for item in sorted(self.results.items.get("job-1", []), key=lambda i: i.rank)]

    def with_reason(self, reason: str) -> set[str]:
        return {row.candidate_id for row in self.results.excluded["job-1"] if row.decision_reason == reason}

    async def test_best_r_get_cards_when_big_batch_fails(self) -> None:
        # top_n=8, 12 R: пакет из 8 обрывается, повтор частями не больше 5 доводится
        self.build(12, fail_above=5)
        await self.narrate(8, retry=True)
        self.assertEqual(self.shown(), self.ids(0, 8))  # лучшие 8 по вероятности и в её порядке
        self.assertTrue(all(i.title_ru.startswith("Карточка:") for i in self.results.items["job-1"]))
        self.assertEqual(self.with_reason("RUBRIC_TEXT_FAILED"), set())
        self.assertEqual(self.with_reason("RUBRIC_BELOW_TOP_N_R"), set(self.ids(8, 12)))
        self.assertEqual(self.insight.sizes[0], 8)  # сначала — весь пакет одним вызовом
        self.assertTrue(all(size <= 5 for size in self.insight.sizes[1:]))

    async def test_flag_off_best_r_burn_as_before(self) -> None:
        self.build(12, fail_above=5)
        await self.narrate(8, retry=False)
        self.assertEqual(self.with_reason("RUBRIC_TEXT_FAILED"), set(self.ids(0, 8)))
        self.assertEqual(self.shown(), self.ids(8, 12))  # показаны следующие по вероятности
        self.assertEqual(self.insight.sizes, [8, 4])  # повтора нет

    async def test_failed_part_waits_for_next_round_and_keeps_rank(self) -> None:
        # падает и первая малая часть (с лучшим кандидатом): вторая часть принята сразу, кандидаты упавшей
        # доводятся в следующем раунде и стоят выше следующих по вероятности
        self.build(12, fail_above=5)
        self.insight.flaky_once = {self.candidates[0].candidate_id}
        await self.narrate(8, retry=True)
        self.assertEqual(self.shown(), self.ids(0, 8))
        self.assertEqual(self.with_reason("RUBRIC_TEXT_FAILED"), set())
        rounds = self.results.traces["job-1"]["narrate"]["rounds"]
        self.assertEqual(rounds[0]["finalized"], sorted(self.ids(5, 8)))  # частичный результат
        self.assertEqual(rounds[0]["requeued"], self.ids(0, 5))
        self.assertEqual(rounds[1]["finalized"], sorted(self.ids(0, 5)))

    async def test_part_failing_again_gets_text_failed_and_next_fill(self) -> None:
        # часть со «сломанным» кандидатом падает и в следующем раунде: RUBRIC_TEXT_FAILED после повтора,
        # третий раунд добирает следующих по вероятности
        self.build(12, fail_above=5)
        self.insight.broken = {self.candidates[0].candidate_id}
        await self.narrate(8, retry=True)
        self.assertEqual(self.with_reason("RUBRIC_TEXT_FAILED"), set(self.ids(0, 5)))
        self.assertEqual(self.shown(), self.ids(5, 12))
        self.assertEqual(len(self.results.traces["job-1"]["narrate"]["rounds"]), 3)

    async def test_retry_does_not_start_after_deadline(self) -> None:
        # срок задания: после упавшего вызова повтор частями не начинается, лишних вызовов insight нет
        self.build(12, fail_above=5)
        await self.narrate(8, retry=True, llm_allowed=lambda: not self.insight.sizes)
        self.assertEqual(self.insight.sizes, [8])
        trace = self.results.traces["job-1"]["narrate"]
        self.assertTrue(trace["deadline_cut"])
        self.assertEqual(self.shown(), self.ids(0, 8))  # резервная ветка, как и до правки

    async def test_batch_over_30_is_split(self) -> None:
        # top_n=50: insight доводит не больше 30 карточек вызова — пакет режется на вызовы по ≤ 30
        self.build(80)
        await self.narrate(50, retry=True)
        self.assertLessEqual(max(self.insight.sizes), INSIGHT_MAX_CARDS)
        self.assertEqual(self.shown(), self.ids(0, 50))
        self.assertEqual(self.with_reason("RUBRIC_TEXT_FAILED"), set())

    async def test_flag_off_batch_over_30_loses_rest_as_before(self) -> None:
        self.build(80)
        await self.narrate(50, retry=False)
        self.assertEqual(self.insight.sizes[0], 50)
        self.assertEqual(self.with_reason("RUBRIC_TEXT_FAILED"), set(self.ids(30, 50)))
        self.assertEqual(self.shown(), self.ids(0, 30) + self.ids(50, 70))


class FlagPlumbingTest(unittest.TestCase):
    def test_flag_is_off_by_default_and_reaches_job_config(self) -> None:
        self.assertFalse(NarrateConfig().rubric_finalize_retry_small)
        self.assertFalse(_narrate_config(NarrateConfig(), 15, None).rubric_finalize_retry_small)
        self.assertTrue(_narrate_config(NarrateConfig(rubric_finalize_retry_small=True), 15, None)
                        .rubric_finalize_retry_small)


if __name__ == "__main__":
    unittest.main()
