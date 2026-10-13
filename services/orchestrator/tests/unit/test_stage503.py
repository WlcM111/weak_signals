"""Этап 5.0.3: у анализа нет отдельного лимита — его ограничивает только срок задания."""

from __future__ import annotations

import unittest
from orchestrator.application.stages.analyze import UNBOUNDED_ANALYSIS_SECONDS, AnalyzeConfig, analysis_timeout


class AnalysisLimitTest(unittest.TestCase):
    def test_zero_means_job_deadline_only(self) -> None:
        self.assertEqual(analysis_timeout(AnalyzeConfig(timeout_seconds=0), 1456), UNBOUNDED_ANALYSIS_SECONDS)
        self.assertEqual(analysis_timeout(AnalyzeConfig(timeout_seconds=600, per_document_seconds=0.5), 1456), 728)


if __name__ == "__main__":
    unittest.main()
