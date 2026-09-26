"""Адаптер Роспатента на ответе из примера документации API, без сети; классификация документов как патентов.

Фикстура повторяет поля примера ответа `POST /search` из документа Роспатента «Описание методов
программных интерфейсов Системы (API)» (2022); два последних элемента — заведомо негодные записи.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from collector.adapters.outbound.rules_loader import load_classification_config
from collector.adapters.outbound.sources.rospatent import (
    API_URL,
    RospatentAdapter,
    parse_publication_date,
    query_plan,
    request_body,
)
from collector.domain.classify import classify
from collector.domain.errors import AdapterFailure
from collector.domain.values import (
    AdapterErrorCode,
    CollectionLimits,
    CollectionMode,
    SearchTerms,
    SourceKey,
    SourceType,
    TrustLevel,
)

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "http" / "rospatent" / "search_success.json"
SERVICE_ROOT = Path(__file__).resolve().parents[2]  # services/collector: config/ без импорта настроек сервиса
TERMS = SearchTerms(ru=("ракета", "ракетный двигатель"), en=("rocket", "rocket engine"))
LIMITS = replace(CollectionLimits.defaults(CollectionMode.SEARCH), published_since_year=2000)
CONFIG = load_classification_config(SERVICE_ROOT / "config" / "trust_rules.yaml",
                                    SERVICE_ROOT / "config" / "rss_domains.yaml")


class RospatentAdapterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.http = FakeHttpClient()

    async def collect(self, terms: SearchTerms = TERMS, limits: CollectionLimits = LIMITS) -> list:  # type: ignore[type-arg]
        adapter = RospatentAdapter(self.http, FakeRateLimiter(), FakeClock(), "секретный-ключ")
        return [document async for document in adapter.search(terms, limits, 10_000.0)]

    async def test_parses_documents_from_documentation_example(self) -> None:
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.http.enqueue({"total": 0, "available": 0, "hits": []})
        documents = await self.collect()
        self.assertEqual([d.url for d in documents], [
            "https://searchplatform.rospatent.gov.ru/doc/UA0000028083C2_20001016",
            "https://searchplatform.rospatent.gov.ru/doc/RU2625135C1_20170711",
        ])
        first, second = documents
        self.assertEqual(first.title, "РАКЕТА СО СТАБИЛИЗАТОРОМ В СОПЛЕ")
        self.assertIn("<em>ракеты</em>", first.text)  # HTML снимает нормализация collector
        self.assertEqual(first.language_code, "ru")
        self.assertEqual(first.matched_term, "ракета")
        self.assertEqual(first.published_at.isoformat(), "2000-10-16T00:00:00+00:00")  # type: ignore[union-attr]
        self.assertEqual(first.raw_meta, {"ipc": ["F02K9/00"], "applicant": ["Омельяненко Юрий Петрович (UA)"],
                                          "kind": "C2", "publishing_office": "UA", "dataset": "cis"})
        # Русского названия нет — берётся английское; заявителей в biblio нет — строка из фрагмента пуста.
        self.assertEqual(second.title, "METHOD OF STEAM START OF ANTI-AIRCRAFT MISSILES")
        self.assertEqual(second.raw_meta["applicant"], [])
        self.assertEqual(second.raw_meta["ipc"], ["F42B15/00"])

    async def test_request_is_post_with_bearer_key_and_date_filter(self) -> None:
        self.http.enqueue({"hits": []})
        self.http.enqueue({"hits": []})
        await self.collect()
        self.assertEqual([call[0] for call in self.http.calls], [API_URL, API_URL])
        first, second = (call[1] for call in self.http.calls)
        self.assertEqual(first["headers"], {"Authorization": "Bearer секретный-ключ"})
        self.assertEqual(first["json"], {"qn": "ракета", "limit": 25,
                                         "filter": {"date_published": {"range": {"gte": "20000101"}}}})
        self.assertEqual(second["json"]["qn"], "rocket")
        self.assertNotIn("секретный-ключ", json.dumps(first["json"], ensure_ascii=False))

    async def test_same_document_from_two_phrases_is_yielded_once(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.http.enqueue(payload)
        self.http.enqueue(payload)
        documents = await self.collect()
        self.assertEqual(len(documents), 2)

    async def test_old_documents_are_filtered_by_year(self) -> None:
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        self.http.enqueue({"hits": []})
        documents = await self.collect(limits=replace(LIMITS, published_since_year=2010))
        self.assertEqual([d.raw_meta["publishing_office"] for d in documents], ["RU"])

    async def test_limit_per_source_is_respected(self) -> None:
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        documents = await self.collect(limits=replace(LIMITS, max_documents_per_source=1))
        self.assertEqual(len(documents), 1)
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(self.http.calls[0][1]["json"]["limit"], 1)

    async def test_malformed_payload_is_parse_error(self) -> None:
        self.http.enqueue({"hits": "не список"})
        with self.assertRaises(AdapterFailure) as error:
            await self.collect()
        self.assertEqual(error.exception.code, AdapterErrorCode.PARSE_ERROR.value)

    async def test_rejected_key_is_http_4xx(self) -> None:
        self.http.enqueue(status_error(401))
        with self.assertRaises(AdapterFailure) as error:
            await self.collect()
        self.assertEqual(error.exception.code, AdapterErrorCode.HTTP_4XX.value)

    def test_empty_token_is_value_error(self) -> None:
        with self.assertRaises(ValueError):
            RospatentAdapter(self.http, FakeRateLimiter(), FakeClock(), "   ")

    def test_query_plan_takes_first_phrase_of_each_language(self) -> None:
        self.assertEqual(query_plan(TERMS), [("ракета", "ru"), ("rocket", "en")])
        self.assertEqual(query_plan(SearchTerms(en=("rocket",))), [("rocket", "en")])
        self.assertEqual(request_body("x", replace(LIMITS, max_documents_per_source=150))["limit"], 25)

    def test_publication_date_formats(self) -> None:
        self.assertEqual(parse_publication_date("2016.07.10").isoformat(), "2016-07-10T00:00:00+00:00")
        self.assertEqual(parse_publication_date("19990310").isoformat(), "1999-03-10T00:00:00+00:00")
        self.assertIsNone(parse_publication_date("10.07.2016"))
        self.assertIsNone(parse_publication_date(None))


class RospatentClassificationTest(unittest.TestCase):
    def test_patent_on_gov_ru_domain_stays_patent(self) -> None:
        source_type, trust = classify(CONFIG, SourceKey.ROSPATENT, "searchplatform.rospatent.gov.ru",
                                      "СПОСОБ ПОЛУЧЕНИЯ ПЕРФТОРУГЛЕРОДОВ", has_published_at=True)
        self.assertEqual((source_type, trust), (SourceType.PATENT, TrustLevel.HIGH))

    def test_other_sources_on_gov_ru_are_still_government(self) -> None:
        source_type, _ = classify(CONFIG, SourceKey.OPENALEX, "searchplatform.rospatent.gov.ru",
                                  "title", has_published_at=True)
        self.assertEqual(source_type, SourceType.GOVERNMENT)

    def test_only_rospatent_is_authoritative(self) -> None:
        self.assertEqual(CONFIG.authoritative_sources, frozenset({SourceKey.ROSPATENT}))


if __name__ == "__main__":
    unittest.main()
