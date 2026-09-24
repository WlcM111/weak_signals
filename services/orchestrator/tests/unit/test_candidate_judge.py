"""Смысловая оценка кандидатов перед нарративом: отсев, порядок, отказоустойчивость, флаг и срок.

Причина изменения (прогоны 22–24.09, 19 тем, ручная разметка): обзоры, общие понятия и статьи не по теме
проходили в выдачу и с источниками высокой доверенности — ни одна стадия не оценивала кандидата по смыслу.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from orchestrator.application.dto import CandidateView, JudgeVerdictView
from orchestrator.application.stages.narrate import apply_judgement
from orchestrator.domain.values import Decision

from .test_run_job import RunJobHarness


def candidate(rank: int) -> CandidateView:
    return CandidateView(f"c-{rank}", rank, f"Кандидат {rank}", (), 0.9, Decision.WEAK_SIGNAL, "MODEL_SCORE", "", 3)


def verdict(rank: int, kind: str, relevance: int) -> tuple[str, JudgeVerdictView]:
    return f"c-{rank}", JudgeVerdictView(f"c-{rank}", kind, relevance, "Причина.")


class ApplyJudgementTest(unittest.TestCase):
    def test_rejects_non_technology_verdicts_and_zero_relevance(self) -> None:
        weak = [candidate(r) for r in range(1, 8)]
        verdicts = dict([
            verdict(1, "OVERVIEW", 1), verdict(2, "GENERIC_CONCEPT", 1), verdict(3, "OFF_TOPIC", 0),
            verdict(4, "NOISE", 0), verdict(5, "MATURE_TECHNOLOGY", 3), verdict(6, "EMERGING_TECHNOLOGY", 0),
            verdict(7, "EMERGING_TECHNOLOGY", 3),
        ])
        kept, rejected = apply_judgement(weak, verdicts)
        self.assertEqual([c.candidate_id for c in kept], ["c-7"])
        self.assertEqual(len(rejected), 6)

    def test_order_by_relevance_then_rank_and_unjudged_last(self) -> None:
        weak = [candidate(r) for r in range(1, 5)]
        verdicts = dict([verdict(1, "EMERGING_TECHNOLOGY", 1), verdict(2, "EMERGING_TECHNOLOGY", 3),
                         verdict(3, "EMERGING_TECHNOLOGY", 3)])
        kept, rejected = apply_judgement(weak, verdicts)
        self.assertEqual([c.candidate_id for c in kept], ["c-2", "c-3", "c-1", "c-4"])
        self.assertEqual(rejected, [])


class JudgeInRunJobTest(RunJobHarness):
    async def run_query(self, key: str, **insight_settings: object) -> object:
        """Одна сборка, настройка дублёра insight, затем задание."""
        run_job, job, query = self.build(top_n=2)
        for name, value in insight_settings.items():
            setattr(self.insight, name, value)
        stored = await self.jobs.insert(query, job, key, "hash", self.clock.now())
        await run_job.execute(stored, "worker-1")
        return stored

    async def test_overview_candidate_excluded_with_reason_and_ranks_renumbered(self) -> None:
        stored = await self.run_query(
            "key-j001:submit",
            judge_rule=lambda c: ("OVERVIEW", 1) if c.rank == 1 else ("EMERGING_TECHNOLOGY", 2),
        )
        excluded = [e for e in self.results.excluded.get(stored.job_id, []) if e.decision_reason == "SEMANTIC_JUDGE"]
        self.assertEqual(len(excluded), 1)
        self.assertIs(excluded[0].decision, Decision.INSUFFICIENT_EVIDENCE)
        self.assertIn("обзор", excluded[0].decision_explanation_ru)
        items = self.results.items.get(stored.job_id, [])
        self.assertNotIn(excluded[0].candidate_id, {item.candidate_id for item in items})
        self.assertEqual([item.rank for item in items], list(range(1, len(items) + 1)))

    async def test_judge_failure_keeps_previous_behaviour(self) -> None:
        stored = await self.run_query("key-j002:submit", fail_judge=True)
        self.assertGreater(len(self.results.items.get(stored.job_id, [])), 0)
        excluded = self.results.excluded.get(stored.job_id, [])
        self.assertFalse(any(e.decision_reason == "SEMANTIC_JUDGE" for e in excluded))

    async def test_judge_disabled_by_config(self) -> None:
        run_job, job, query = self.build(top_n=2)
        narrate = replace(run_job._config.narrate, judge_enabled=False)  # noqa: SLF001
        run_job._config = replace(run_job._config, narrate=narrate)  # noqa: SLF001
        stored = await self.jobs.insert(query, job, "key-j003:submit", "hash", self.clock.now())
        await run_job.execute(stored, "worker-1")
        self.assertEqual(self.insight.judge_calls, [])

    async def test_judge_skipped_when_no_time_for_llm(self) -> None:
        run_job, job, query = self.build(top_n=2)
        stored = await self.jobs.insert(query, job, "key-j004:submit", "hash", self.clock.now())
        self.clock.advance(1200 - 120 - 60)
        await run_job.execute(stored, "worker-1")
        self.assertEqual(self.insight.judge_calls, [])


if __name__ == "__main__":
    unittest.main()
