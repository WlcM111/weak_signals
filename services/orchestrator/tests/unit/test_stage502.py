"""Этап 5.0.2: ошибка сервиса не выдаётся за недоступность, судья — пачками до 40."""

from __future__ import annotations

import unittest

from orchestrator.application.stages.analyze import upstream_failure
from orchestrator.application.stages.narrate import JUDGE_BATCH, NarrateConfig, _judge
from orchestrator.domain.errors import TRANSIENT_GRPC_CODES, UpstreamUnavailable
from orchestrator.domain.values import JobErrorCode


def _fail(rpc: str, code: str) -> UpstreamUnavailable:
    """Как clients._fail: код ответа сервиса определяет, временная ли это недоступность."""
    return UpstreamUnavailable(f"{rpc}: {code}", transient=code in TRANSIENT_GRPC_CODES)


class FailureKindTest(unittest.TestCase):
    def test_unknown_is_a_service_error_and_unavailable_is_transient(self) -> None:
        unknown = _fail("StartAnalysis", "UNKNOWN")
        stage = upstream_failure("analyzer", JobErrorCode.ANALYSIS_FAILED, unknown)
        self.assertEqual((stage.error_code, stage.retryable), ("ANALYSIS_FAILED", False))
        self.assertIn("вернул ошибку", stage.message)
        down = upstream_failure("analyzer", JobErrorCode.ANALYSIS_FAILED,
                                _fail("StartAnalysis", "UNAVAILABLE"))
        self.assertEqual((down.error_code, down.retryable), ("UPSTREAM_UNAVAILABLE", True))


class JudgeBatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_pool_above_insight_limit_is_split(self) -> None:
        class Insight:
            def __init__(self) -> None:
                self.sizes = []

            async def judge_candidates(self, query, candidates):  # noqa: ANN001, ANN201
                self.sizes.append(len(candidates))
                return {c: object() for c in candidates}

        insight = Insight()
        verdicts = await _judge(insight, "q", [f"c{i}" for i in range(60)], NarrateConfig(judge_enabled=True, judge_pool=60))
        self.assertEqual((insight.sizes, len(verdicts)), ([JUDGE_BATCH, 60 - JUDGE_BATCH], 60))
        self.assertLessEqual(max(insight.sizes), 40)


if __name__ == "__main__":
    unittest.main()
