"""U-06 / R-073: причина PARTIAL доходит до API (прежде отдавалась только при коде ошибки, а у PARTIAL кода нет)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from orchestrator.adapters.inbound.http.presenters import job_to_json
from orchestrator.domain.values import JobStatus

PARTIAL_MESSAGE = "PARTIAL:found=14<50;adapters_failed=openalex;collection=PARTIAL"  # задание edge, jobs51


def _view(status: JobStatus, error_code: str | None, error_message: str | None) -> SimpleNamespace:
    moment = datetime(2026, 10, 14, 1, 57, tzinfo=UTC)
    job = SimpleNamespace(job_id="j", query_id="q", query_text="edge", status=status, attempt=1, cancel_requested=False,
                          created_at=moment, started_at=moment, finished_at=moment, error_code=error_code,
                          error_message=error_message)
    progress = SimpleNamespace(stage=status, http_requests_total=325, sources_processed=11, documents_collected=1729,
                               candidates_found=150, narratives_done=14, narratives_total=14)
    return SimpleNamespace(job=job, progress=progress)


class PartialReasonTest(unittest.TestCase):
    def test_partial_reason_is_returned(self) -> None:
        payload = job_to_json(_view(JobStatus.PARTIAL, None, PARTIAL_MESSAGE))
        self.assertEqual(payload["error"]["code"], "PARTIAL_RESULT")
        self.assertEqual(payload["error"]["message"], PARTIAL_MESSAGE)

    def test_completed_and_failed_unchanged(self) -> None:
        self.assertNotIn("error", job_to_json(_view(JobStatus.COMPLETED, None, None)))
        failed = job_to_json(_view(JobStatus.FAILED, "ANALYSIS_FAILED", "анализ не завершился"))
        self.assertEqual(failed["error"]["code"], "ANALYSIS_FAILED")


if __name__ == "__main__":
    unittest.main()
