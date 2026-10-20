"""20.10.2026: фильтр по дате публикации — научный кандидат без свежих источников исключается до LLM;
кандидат с источником СМИ, без дат или при выключенном фильтре не трогается."""

from __future__ import annotations

import dataclasses
import unittest
from datetime import UTC, date, datetime

from orchestrator.application.stages import rubric
from orchestrator.domain.values import Decision

from .test_rubric_selection import doc

TODAY = date(2026, 10, 20)


def dated(index: int, source_type: str, published: str | None):  # noqa: ANN201
    when = datetime.fromisoformat(published).replace(tzinfo=UTC) if published else None
    return dataclasses.replace(doc(index, source_type), published_at=when)


class StaleFilterTest(unittest.TestCase):
    def test_old_research_only_candidate_is_excluded_as_mature(self) -> None:
        docs = [dated(1, "SCIENTIFIC_PUBLICATION", "2024-11-01"), dated(2, "PREPRINT", "2025-03-01")]
        verdict = rubric.stale_verdict(docs, 540, TODAY)
        self.assertIsNotNone(verdict)
        decision, reason, explanation = verdict  # type: ignore[misc]
        self.assertEqual((decision, reason), (Decision.MATURE, "STALE_PUBLICATIONS"))
        self.assertIn("01.03.2025", explanation)
        self.assertIn("порог 540", explanation)

    def test_fresh_source_keeps_candidate(self) -> None:
        docs = [dated(1, "SCIENTIFIC_PUBLICATION", "2023-01-01"), dated(2, "PREPRINT", "2026-06-01")]
        self.assertIsNone(rubric.stale_verdict(docs, 540, TODAY))

    def test_boundary_is_inclusive_for_keeping(self) -> None:
        docs = [dated(1, "SCIENTIFIC_PUBLICATION", "2025-04-28")]  # ровно 540 дней — ещё не старше порога
        self.assertEqual((TODAY - date(2025, 4, 28)).days, 540)
        self.assertIsNone(rubric.stale_verdict(docs, 540, TODAY))

    def test_market_source_protects_and_missing_dates_or_zero_threshold_do_nothing(self) -> None:
        old_paper = dated(1, "SCIENTIFIC_PUBLICATION", "2022-01-01")
        self.assertIsNone(rubric.stale_verdict([old_paper, dated(2, "NEWS", "2022-01-01")], 540, TODAY))
        self.assertIsNone(rubric.stale_verdict([dated(3, "SCIENTIFIC_PUBLICATION", None)], 540, TODAY))
        self.assertIsNone(rubric.stale_verdict([old_paper], 0, TODAY))
        self.assertIsNone(rubric.stale_verdict([], 540, TODAY))


if __name__ == "__main__":
    unittest.main()
