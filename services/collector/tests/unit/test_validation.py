"""Правила валидации RPC (§10.1 ТЗ, CONTRACT_RULES.md)."""

from __future__ import annotations

import unittest

from collector.application.validation import (
    validate_cancel_reason,
    validate_chunk_size,
    validate_document_ids,
    validate_encyclopedia_request,
    validate_idempotency_key,
    validate_limits,
    validate_mode,
    validate_page_token,
    validate_query_text,
    validate_sources,
    validate_terms,
    validate_uuid,
)
from collector.domain.errors import ValidationError
from collector.domain.rules import encode_page_token
from collector.domain.values import CollectionMode, SourceKey

VALID_UUID = "0192f0a1-2b3c-7def-8123-456789abcdef"


class ValidationTest(unittest.TestCase):
    """Каждое правило проверяется на допустимом и недопустимом значении."""

    def assert_code(self, code: str, callable_, *args) -> None:  # noqa: ANN001 - вспомогательный помощник
        with self.assertRaises(ValidationError) as ctx:
            callable_(*args)
        self.assertEqual(ctx.exception.error_code, code)

    def test_uuid(self) -> None:
        self.assertEqual(validate_uuid(VALID_UUID, "collection_id"), VALID_UUID)
        self.assert_code("INVALID_UUID", validate_uuid, "not-a-uuid", "collection_id")
        self.assert_code("INVALID_UUID", validate_uuid, "", "collection_id")

    def test_idempotency_key(self) -> None:
        self.assertEqual(validate_idempotency_key("job-1:collect"), "job-1:collect")
        self.assert_code("INVALID_IDEMPOTENCY_KEY", validate_idempotency_key, "short")
        self.assert_code("INVALID_IDEMPOTENCY_KEY", validate_idempotency_key, "x" * 129)
        self.assert_code("INVALID_IDEMPOTENCY_KEY", validate_idempotency_key, "ключ с пробелом")

    def test_query_text(self) -> None:
        self.assertEqual(validate_query_text("  технологии в ИИ  "), "технологии в ИИ")
        self.assert_code("INVALID_QUERY", validate_query_text, " ")
        self.assert_code("INVALID_QUERY", validate_query_text, "!!!")
        self.assert_code("INVALID_QUERY", validate_query_text, "a" * 501)

    def test_terms(self) -> None:
        terms = validate_terms([" защита ИИ "], ["ai security"])
        self.assertEqual(terms.ru, ("защита ИИ",))
        self.assertEqual(terms.en, ("ai security",))
        self.assert_code("INVALID_TERMS", validate_terms, [], [])
        self.assert_code("INVALID_TERMS", validate_terms, ["a"], [])
        self.assert_code("INVALID_TERMS", validate_terms, ["x" * 121], [])
        self.assert_code("INVALID_TERMS", validate_terms, [f"тема {i}" for i in range(9)], [])
        self.assert_code("INVALID_TERMS", validate_terms, ["тема", "ТЕМА"], [])

    def test_limits_defaults_by_mode(self) -> None:
        search = validate_limits(CollectionMode.SEARCH, 0, 0, 0, 0)
        self.assertEqual(
            (search.max_documents_per_source, search.max_total_documents, search.time_budget_seconds),
            (150, 800, 120),
        )
        enrichment = validate_limits(CollectionMode.ENRICHMENT, 0, 0, 0, 0)
        self.assertEqual(
            (
                enrichment.max_documents_per_source,
                enrichment.max_total_documents,
                enrichment.time_budget_seconds,
                enrichment.published_since_year,
            ),
            (40, 200, 45, 2023),
        )

    def test_limits_ranges(self) -> None:
        self.assert_code("INVALID_LIMITS", validate_limits, CollectionMode.SEARCH, 501, 0, 0, 0)
        self.assert_code("INVALID_LIMITS", validate_limits, CollectionMode.SEARCH, 0, 3001, 0, 0)
        self.assert_code("INVALID_LIMITS", validate_limits, CollectionMode.SEARCH, 0, 0, 9, 0)
        self.assert_code("INVALID_LIMITS", validate_limits, CollectionMode.SEARCH, 0, 0, 0, 1999)

    def test_mode_and_sources(self) -> None:
        self.assertIs(validate_mode(CollectionMode.SEARCH), CollectionMode.SEARCH)
        self.assert_code("INVALID_ENUM", validate_mode, None)
        self.assertEqual(validate_sources([]), ())
        self.assertEqual(validate_sources([SourceKey.ARXIV]), (SourceKey.ARXIV,))
        self.assert_code("INVALID_ENUM", validate_sources, [None])
        self.assert_code("INVALID_ENUM", validate_sources, [SourceKey.ARXIV, SourceKey.ARXIV])

    def test_chunk_size_and_page_token(self) -> None:
        self.assertEqual(validate_chunk_size(0), 50)
        self.assertEqual(validate_chunk_size(200), 200)
        self.assert_code("INVALID_LIMITS", validate_chunk_size, 201)
        self.assertIsNone(validate_page_token(""))
        self.assertEqual(validate_page_token(encode_page_token(7, VALID_UUID)), (7, VALID_UUID))
        self.assert_code("INVALID_PAGE_TOKEN", validate_page_token, "подделка")

    def test_document_ids(self) -> None:
        self.assertEqual(validate_document_ids([VALID_UUID]), (VALID_UUID,))
        self.assert_code("INVALID_ARGUMENT", validate_document_ids, [])
        self.assert_code("INVALID_ARGUMENT", validate_document_ids, [VALID_UUID] * 201)
        self.assert_code("INVALID_UUID", validate_document_ids, ["нет"])

    def test_encyclopedia_request(self) -> None:
        titles, language = validate_encyclopedia_request([" Kubernetes "], "RU")
        self.assertEqual((titles, language), (("Kubernetes",), "ru"))
        self.assert_code("INVALID_ARGUMENT", validate_encyclopedia_request, [], "ru")
        self.assert_code("INVALID_ARGUMENT", validate_encyclopedia_request, ["x"] * 21, "ru")
        self.assert_code("INVALID_ARGUMENT", validate_encyclopedia_request, [" "], "ru")
        self.assert_code("INVALID_ENUM", validate_encyclopedia_request, ["Kubernetes"], "de")

    def test_cancel_reason(self) -> None:
        self.assertEqual(validate_cancel_reason("  не нужно  "), "не нужно")
        self.assert_code("INVALID_ARGUMENT", validate_cancel_reason, "x" * 201)


if __name__ == "__main__":
    unittest.main()
