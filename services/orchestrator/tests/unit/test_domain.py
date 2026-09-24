"""Домен orchestrator: переходы статусов, повторы, инварианты снимка, правило завершения."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from orchestrator.application.validation import (
    canonical_request_hash,
    check_api_key,
    decode_cursor,
    encode_cursor,
    hash_client_ip,
    validate_idempotency_key,
    validate_page_size,
    validate_query_text,
    validate_score_request,
    validate_top_n,
    validate_uuid,
)
from orchestrator.domain.entities import (
    ExcludedCandidate,
    FeatureRow,
    Job,
    Query,
    ResultItem,
    ResultSnapshot,
    SourceRow,
    check_ranks,
)
from orchestrator.domain.errors import InvariantViolation, ValidationError
from orchestrator.domain.rules import decide_completion
from orchestrator.domain.values import (
    ConfidenceBand,
    Decision,
    FeatureDirection,
    JobErrorCode,
    JobStatus,
    NarrativeStatus,
    SummaryKind,
    TrustLevel,
    normalize_query,
)

from ..fakes import new_id

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def make_job(status: JobStatus = JobStatus.QUEUED, attempt: int = 0) -> Job:
    """Задание в нужном статусе."""
    return Job(job_id=new_id(1), query_id=new_id(2), status=status, attempt=attempt, created_at=NOW)


def make_item(rank: int, candidate_index: int = 1, score: float = 0.9) -> ResultItem:
    """Элемент выдачи для проверки инвариантов."""
    return ResultItem(
        job_id=new_id(1),
        rank=rank,
        candidate_id=new_id(200 + candidate_index),
        title_ru=f"Технология {rank}",
        title_auto=f"Кандидат {rank}",
        score=score,
        decision_reason="MODEL_SCORE",
        decision_explanation_ru="Объяснение",
        description_ru="Описание",
        advantage_ru="Преимущество",
        case_example_ru="Кейс",
        explanation_ru="Почему слабый сигнал",
        narrative_status=NarrativeStatus.GENERATED,
        llm_provider="gigachat",
        llm_model="GigaChat-2-Pro",
        prompt_version="insight_v1",
        document_count=5,
    )


class QueryTest(unittest.TestCase):
    """Нормализация текста и проверки диапазонов."""

    def test_normalize_collapses_spaces_and_case(self) -> None:
        self.assertEqual(normalize_query("  Слабые   СИГНАЛЫ  в ИИ "), "слабые сигналы в ии")

    def test_create_sets_normalized_text(self) -> None:
        query = Query.create(new_id(3), "  Технологии   в ИИ  ", 15)
        self.assertEqual(query.text, "Технологии в ИИ")
        self.assertEqual(query.normalized_text, "технологии в ии")

    def test_rejects_short_text(self) -> None:
        with self.assertRaises(InvariantViolation):
            Query.create(new_id(3), "и", 15)

    def test_rejects_top_n_out_of_range(self) -> None:
        with self.assertRaises(InvariantViolation):
            Query.create(new_id(3), "технологии в ИИ", 51)


class JobTransitionTest(unittest.TestCase):
    """Таблица переходов статусов задания (§6 HANDOFF)."""

    ALLOWED = (
        (JobStatus.QUEUED, JobStatus.COLLECTING),
        (JobStatus.QUEUED, JobStatus.CANCELLED),
        (JobStatus.COLLECTING, JobStatus.ANALYZING),
        (JobStatus.COLLECTING, JobStatus.FAILED),
        (JobStatus.COLLECTING, JobStatus.CANCELLED),
        (JobStatus.COLLECTING, JobStatus.QUEUED),
        (JobStatus.ANALYZING, JobStatus.NARRATING),
        (JobStatus.ANALYZING, JobStatus.FAILED),
        (JobStatus.ANALYZING, JobStatus.CANCELLED),
        (JobStatus.ANALYZING, JobStatus.QUEUED),
        (JobStatus.NARRATING, JobStatus.COMPLETED),
        (JobStatus.NARRATING, JobStatus.PARTIAL),
        (JobStatus.NARRATING, JobStatus.CANCELLED),
        (JobStatus.NARRATING, JobStatus.QUEUED),
    )
    FORBIDDEN = (
        (JobStatus.QUEUED, JobStatus.ANALYZING),
        (JobStatus.QUEUED, JobStatus.COMPLETED),
        (JobStatus.COLLECTING, JobStatus.NARRATING),
        (JobStatus.COMPLETED, JobStatus.QUEUED),
        (JobStatus.FAILED, JobStatus.COLLECTING),
        (JobStatus.CANCELLED, JobStatus.COLLECTING),
        (JobStatus.PARTIAL, JobStatus.NARRATING),
    )

    def test_allowed_transitions(self) -> None:
        for source, target in self.ALLOWED:
            with self.subTest(source=source, target=target):
                job = make_job(source)
                job.transition(target, NOW)
                self.assertIs(job.status, target)

    def test_forbidden_transitions(self) -> None:
        for source, target in self.FORBIDDEN:
            with self.subTest(source=source, target=target):
                job = make_job(source)
                with self.assertRaises(InvariantViolation):
                    job.transition(target, NOW)

    def test_terminal_sets_finished_at(self) -> None:
        job = make_job(JobStatus.NARRATING)
        job.transition(JobStatus.COMPLETED, NOW)
        self.assertEqual(job.finished_at, NOW)
        self.assertIsNone(job.lease)

    def test_collecting_sets_started_at_once(self) -> None:
        job = make_job()
        job.transition(JobStatus.COLLECTING, NOW)
        first = job.started_at
        job.transition(JobStatus.QUEUED, NOW)
        job.transition(JobStatus.COLLECTING, NOW)
        self.assertEqual(job.started_at, first)

    def test_cancel_sets_code(self) -> None:
        job = make_job(JobStatus.ANALYZING)
        job.cancel(NOW)
        self.assertIs(job.status, JobStatus.CANCELLED)
        self.assertEqual(job.error_code, JobErrorCode.CANCELLED_BY_USER.value)


class RequeueTest(unittest.TestCase):
    """Повторные попытки ограничены тремя."""

    def test_requeue_increments_attempt(self) -> None:
        job = make_job(JobStatus.COLLECTING)
        self.assertTrue(job.requeue(NOW))
        self.assertEqual(job.attempt, 1)
        self.assertIs(job.status, JobStatus.QUEUED)

    def test_fourth_attempt_fails_job(self) -> None:
        job = make_job(JobStatus.NARRATING, attempt=3)
        self.assertFalse(job.requeue(NOW))
        self.assertIs(job.status, JobStatus.FAILED)
        self.assertEqual(job.error_code, JobErrorCode.LEASE_EXPIRED_MAX_ATTEMPTS.value)

    def test_three_requeues_then_failure(self) -> None:
        job = make_job(JobStatus.COLLECTING)
        for expected in (1, 2, 3):
            self.assertTrue(job.requeue(NOW))
            self.assertEqual(job.attempt, expected)
            job.transition(JobStatus.COLLECTING, NOW)
        self.assertFalse(job.requeue(NOW))


class SnapshotInvariantTest(unittest.TestCase):
    """Инварианты снимка результата."""

    def test_ranks_must_be_continuous(self) -> None:
        check_ranks([make_item(1), make_item(2)])
        with self.assertRaises(InvariantViolation):
            check_ranks([make_item(1), make_item(3)])

    def test_duplicate_candidate_rejected(self) -> None:
        job = make_job(JobStatus.COMPLETED)
        with self.assertRaises(InvariantViolation):
            ResultSnapshot(
                job=job,
                query_text="запрос",
                items=(make_item(1, candidate_index=1), make_item(2, candidate_index=1)),
            )

    def test_score_range(self) -> None:
        with self.assertRaises(InvariantViolation):
            make_item(1, score=1.5)

    def test_confidence_band(self) -> None:
        self.assertIs(make_item(1, score=0.9).confidence_band, ConfidenceBand.HIGH)
        self.assertIs(make_item(1, score=0.6).confidence_band, ConfidenceBand.MEDIUM)
        self.assertIs(make_item(1, score=0.2).confidence_band, ConfidenceBand.LOW)

    def test_key_predictors_limited_and_ordered(self) -> None:
        features = tuple(
            FeatureRow(f"f{index}", f"Признак {index}", 0.5, 0.1, FeatureDirection.NEUTRAL, index)
            for index in range(1, 9)
        )
        item = make_item(1)
        item.features = features
        self.assertEqual(len(item.key_predictors), 5)
        self.assertEqual([row.display_order for row in item.key_predictors], [1, 2, 3, 4, 5])

    def test_source_invariants(self) -> None:
        with self.assertRaises(InvariantViolation):
            SourceRow(
                position=0,
                document_id=new_id(5),
                title="t",
                url="https://e.org",
                source_type="NEWS",
                source_key="rss",
                language_code="ru",
                trust_level=TrustLevel.LOW,
                summary_ru="резюме",
                summary_kind=SummaryKind.ORIGINAL_RU,
                snippet="сниппет",
                similarity=0.5,
            )

    def test_excluded_cannot_be_weak_signal(self) -> None:
        with self.assertRaises(InvariantViolation):
            ExcludedCandidate(
                candidate_id=new_id(9),
                title_auto="Кандидат",
                score=0.9,
                decision=Decision.WEAK_SIGNAL,
                decision_reason="MODEL_SCORE",
                decision_explanation_ru="объяснение",
                document_count=3,
            )


class CompletionTest(unittest.TestCase):
    """Правило выбора COMPLETED или PARTIAL (§7.5 HANDOFF)."""

    def test_completed_when_full(self) -> None:
        result = decide_completion(15, 15, 0, "COMPLETED")
        self.assertIs(result.status, JobStatus.COMPLETED)
        self.assertEqual(result.error_message, "")

    def test_partial_when_not_enough_items(self) -> None:
        result = decide_completion(9, 15, 0, "COMPLETED")
        self.assertIs(result.status, JobStatus.PARTIAL)
        self.assertIn("found=9<15", result.error_message)

    def test_partial_on_fallback_narratives(self) -> None:
        result = decide_completion(15, 15, 2, "COMPLETED")
        self.assertIn("fallback_narratives=2", result.error_message)

    def test_partial_lists_failed_adapters_sorted(self) -> None:
        result = decide_completion(15, 15, 0, "PARTIAL", ["rss", "github"])
        self.assertIn("adapters_failed=github,rss", result.error_message)
        self.assertIn("collection=PARTIAL", result.error_message)

    def test_message_starts_with_prefix(self) -> None:
        self.assertTrue(decide_completion(1, 15, 0, "COMPLETED").error_message.startswith("PARTIAL:"))


class ValidationTest(unittest.TestCase):
    """Валидация HTTP-входа и канонизация тела."""

    def test_query_text(self) -> None:
        self.assertEqual(validate_query_text("  слабые  сигналы "), "слабые сигналы")
        for bad in ("", "и", "   ", "x" * 501):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                validate_query_text(bad)

    def test_top_n(self) -> None:
        self.assertEqual(validate_top_n(None), 15)
        self.assertEqual(validate_top_n(0), 15)
        self.assertEqual(validate_top_n(50), 50)
        with self.assertRaises(ValidationError):
            validate_top_n(51)

    def test_idempotency_key_required(self) -> None:
        self.assertEqual(validate_idempotency_key("job-0001:submit"), "job-0001:submit")
        for bad in (None, "", "short", "плохой ключ", "x" * 129):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                validate_idempotency_key(bad)

    def test_canonical_hash_is_order_independent(self) -> None:
        first = canonical_request_hash({"query_text": "ии", "top_n": 15})
        second = canonical_request_hash({"top_n": 15, "query_text": "ии"})
        self.assertEqual(first, second)

    def test_canonical_hash_changes_with_body(self) -> None:
        self.assertNotEqual(
            canonical_request_hash({"query_text": "ии", "top_n": 15}),
            canonical_request_hash({"query_text": "ии", "top_n": 10}),
        )

    def test_cursor_roundtrip(self) -> None:
        cursor = encode_cursor(NOW, new_id(7))
        self.assertEqual(decode_cursor(cursor), (NOW, new_id(7)))
        self.assertIsNone(decode_cursor(""))

    def test_broken_cursor(self) -> None:
        with self.assertRaises(ValidationError):
            decode_cursor("!!! не курсор !!!")

    def test_page_size(self) -> None:
        self.assertEqual(validate_page_size(None), 20)
        with self.assertRaises(ValidationError):
            validate_page_size(101)

    def test_uuid(self) -> None:
        self.assertEqual(validate_uuid(new_id(1), "job_id"), new_id(1))
        with self.assertRaises(ValidationError):
            validate_uuid("не-uuid", "job_id")

    def test_score_request(self) -> None:
        self.assertEqual(validate_score_request(" Тема ", " текст "), ("Тема", "текст"))
        with self.assertRaises(ValidationError):
            validate_score_request("т", "")

    def test_api_key_comparison(self) -> None:
        self.assertTrue(check_api_key(None, ""))
        self.assertTrue(check_api_key("secret", "secret"))
        self.assertFalse(check_api_key("other", "secret"))
        self.assertFalse(check_api_key(None, "secret"))

    def test_ip_hash_is_stable_and_salted(self) -> None:
        first = hash_client_ip("10.0.0.1", "salt")
        self.assertEqual(first, hash_client_ip("10.0.0.1", "salt"))
        self.assertNotEqual(first, hash_client_ip("10.0.0.1", "other"))
        self.assertIsNone(hash_client_ip(None, "salt"))


if __name__ == "__main__":
    unittest.main()
