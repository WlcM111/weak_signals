"""Инварианты сущностей: переходы статусов, дедупликация, итоговый статус коллекции."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from collector.domain.dedup import DedupIndex, DedupReason
from collector.domain.entities import AdapterRun, Collection, Document, compute_final_status
from collector.domain.errors import InvariantViolation
from collector.domain.values import (
    AdapterErrorCode,
    CollectionLimits,
    CollectionMode,
    OperationStatus,
    SearchTerms,
    SourceKey,
    SourceType,
    TrustLevel,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
DOCUMENT_ID = "11111111-2222-4333-8444-555555555555"


def make_collection(status: OperationStatus = OperationStatus.PENDING) -> Collection:
    """Коллекция в заданном статусе для проверки переходов."""
    return Collection(
        collection_id=DOCUMENT_ID,
        idempotency_key="job-1:collect",
        query_text="защита ИИ",
        mode=CollectionMode.SEARCH,
        terms=SearchTerms(ru=("защита ИИ",)),
        limits=CollectionLimits.defaults(CollectionMode.SEARCH),
        status=status,
    )


class TransitionTest(unittest.TestCase):
    """Матрица переходов статусов коллекции."""

    def test_pending_to_running_and_terminal(self) -> None:
        collection = make_collection()
        collection.transition_to(OperationStatus.RUNNING, NOW)
        self.assertEqual(collection.started_at, NOW)
        collection.finish(OperationStatus.COMPLETED, NOW)
        self.assertEqual(collection.finished_at, NOW)
        self.assertTrue(collection.is_terminal)

    def test_running_can_return_to_pending_on_lease_loss(self) -> None:
        collection = make_collection(OperationStatus.RUNNING)
        collection.transition_to(OperationStatus.PENDING, NOW)
        self.assertIs(collection.status, OperationStatus.PENDING)

    def test_terminal_is_final(self) -> None:
        collection = make_collection(OperationStatus.COMPLETED)
        self.assertFalse(collection.can_transition_to(OperationStatus.RUNNING))
        with self.assertRaises(InvariantViolation):
            collection.transition_to(OperationStatus.RUNNING, NOW)

    def test_pending_cannot_complete_directly(self) -> None:
        with self.assertRaises(InvariantViolation):
            make_collection().finish(OperationStatus.COMPLETED, NOW)

    def test_finish_requires_terminal_status(self) -> None:
        with self.assertRaises(InvariantViolation):
            make_collection(OperationStatus.RUNNING).finish(OperationStatus.RUNNING, NOW)


class FinalStatusTest(unittest.TestCase):
    """Расчёт итогового статуса коллекции (§7 HANDOFF, §9.2 ТЗ)."""

    @staticmethod
    def runs(*statuses: tuple[SourceKey, OperationStatus, str]) -> list[AdapterRun]:
        return [
            AdapterRun(source_key=key, status=status, error_code=code) for key, status, code in statuses
        ]

    def test_all_completed(self) -> None:
        status, code, _ = compute_final_status(
            self.runs((SourceKey.ARXIV, OperationStatus.COMPLETED, "")),
            10,
            cancelled=False,
            budget_exhausted=False,
        )
        self.assertEqual((status, code), (OperationStatus.COMPLETED, ""))

    def test_partial_when_one_adapter_failed(self) -> None:
        status, code, message = compute_final_status(
            self.runs(
                (SourceKey.ARXIV, OperationStatus.COMPLETED, ""),
                (SourceKey.GITHUB, OperationStatus.FAILED, AdapterErrorCode.HTTP_5XX.value),
            ),
            10,
            cancelled=False,
            budget_exhausted=False,
        )
        self.assertEqual(status, OperationStatus.PARTIAL)
        self.assertEqual(code, AdapterErrorCode.HTTP_5XX.value)
        self.assertIn("github", message)

    def test_partial_on_budget_exhausted(self) -> None:
        status, code, _ = compute_final_status(
            self.runs((SourceKey.ARXIV, OperationStatus.COMPLETED, "")),
            5,
            cancelled=False,
            budget_exhausted=True,
        )
        self.assertEqual((status, code), (OperationStatus.PARTIAL, AdapterErrorCode.BUDGET_EXHAUSTED.value))

    def test_failed_without_documents(self) -> None:
        status, code, _ = compute_final_status(
            self.runs((SourceKey.ARXIV, OperationStatus.COMPLETED, "")),
            0,
            cancelled=False,
            budget_exhausted=False,
        )
        self.assertEqual((status, code), (OperationStatus.FAILED, "NO_DOCUMENTS"))

    def test_failed_uses_first_adapter_code(self) -> None:
        status, code, _ = compute_final_status(
            self.runs(
                (SourceKey.ARXIV, OperationStatus.FAILED, AdapterErrorCode.TIMEOUT.value),
                (SourceKey.GITHUB, OperationStatus.FAILED, AdapterErrorCode.HTTP_4XX.value),
            ),
            0,
            cancelled=False,
            budget_exhausted=False,
        )
        self.assertEqual((status, code), (OperationStatus.FAILED, AdapterErrorCode.TIMEOUT.value))

    def test_cancelled_wins(self) -> None:
        status, code, _ = compute_final_status(
            self.runs((SourceKey.ARXIV, OperationStatus.FAILED, AdapterErrorCode.TIMEOUT.value)),
            100,
            cancelled=True,
            budget_exhausted=True,
        )
        self.assertEqual((status, code), (OperationStatus.CANCELLED, "CANCELLED"))


class DedupTest(unittest.TestCase):
    """Три уровня дедупликации внутри одного сбора."""

    def test_url_doi_and_content(self) -> None:
        index = DedupIndex()
        self.assertIsNone(index.register_if_new("url1", "content1", "10.1/a"))
        self.assertIs(index.register_if_new("url1", "other", None), DedupReason.URL)
        self.assertIs(index.register_if_new("url2", "other", "10.1/a"), DedupReason.DOI)
        self.assertIs(index.register_if_new("url3", "content1", None), DedupReason.CONTENT)
        self.assertEqual(index.size, 1)


class DocumentInvariantTest(unittest.TestCase):
    """Инварианты документа (§10.1)."""

    @staticmethod
    def document(**overrides: object) -> Document:
        fields = {
            "document_id": DOCUMENT_ID,
            "url": "https://example.com/a",
            "title": "Заголовок",
            "text": "Текст",
            "language_code": "ru",
            "source_key": SourceKey.RSS,
            "source_type": SourceType.NEWS,
            "trust_level": TrustLevel.MEDIUM,
            "fetched_at": NOW,
            "origin_domain": "example.com",
        }
        fields.update(overrides)
        return Document(**fields)  # type: ignore[arg-type]

    def test_valid_document(self) -> None:
        self.assertEqual(self.document().origin_domain, "example.com")

    def test_rejects_bad_values(self) -> None:
        for overrides in (
            {"url": "ftp://example.com/a"},
            {"title": ""},
            {"text": "x" * 8001},
            {"origin_domain": ""},
            {"citation_count": -1},
            {"engagement_count": -1},
            {"document_id": "short"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(InvariantViolation):
                self.document(**overrides)


class TrustLevelTest(unittest.TestCase):
    """Понижение доверенности до потолка."""

    def test_capped_at(self) -> None:
        self.assertIs(TrustLevel.HIGH.capped_at(TrustLevel.MEDIUM), TrustLevel.MEDIUM)
        self.assertIs(TrustLevel.LOW.capped_at(TrustLevel.MEDIUM), TrustLevel.LOW)


if __name__ == "__main__":
    unittest.main()
