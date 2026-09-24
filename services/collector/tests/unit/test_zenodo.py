"""Адаптер Zenodo на записанных ответах, без сети: разбор, параметры поиска и пауза после отказа."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any

from collector.adapters.outbound.sources import zenodo
from collector.adapters.outbound.sources.zenodo import ZenodoAdapter, reset_zenodo_gate
from collector.application.ports import HttpTimeoutError, HttpTransportError
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, CollectionMode, SearchTerms, SourceKey

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter, status_error

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "http" / "zenodo" / "search_success.json"
TERMS = SearchTerms(ru=("нейроморфные чипы",), en=("neuromorphic chips",))
LIMITS = CollectionLimits.defaults(CollectionMode.SEARCH)
DEADLINE = 10_000.0
EMPTY: dict[str, Any] = {"hits": {"hits": [], "total": 0}}


def preprint(record_id: int) -> dict[str, Any]:
    """Минимальная запись-препринт в формате InvenioRDM."""
    return {
        "id": record_id,
        "links": {"self_html": f"https://zenodo.org/records/{record_id}"},
        "metadata": {
            "title": f"Preprint {record_id}",
            "publication_date": "2026-04-01",
            "resource_type": {"id": "publication-preprint"},
        },
    }


class ZenodoTestCase(unittest.IsolatedAsyncioTestCase):
    """Общая обвязка: свежий шлюз процесса на каждый тест, общие часы для всех экземпляров адаптера."""

    def setUp(self) -> None:
        reset_zenodo_gate()
        self.http = FakeHttpClient()
        self.limiter = FakeRateLimiter()
        self.clock = FakeClock()

    def tearDown(self) -> None:
        reset_zenodo_gate()

    def adapter(self) -> ZenodoAdapter:
        """Новый экземпляр адаптера: шлюз общий на процесс, поэтому пауза переживает экземпляр."""
        return ZenodoAdapter(self.http, self.limiter, self.clock)

    async def collect(self, terms: SearchTerms = TERMS) -> list[Any]:
        """Всё, что выдал адаптер."""
        return [document async for document in self.adapter().search(terms, LIMITS, DEADLINE)]


class ParsingTest(ZenodoTestCase):
    """Разбор обоих форматов записей и отбор препринтов."""

    async def test_parses_both_formats_and_keeps_only_valid_preprints(self) -> None:
        self.http.enqueue(json.loads(FIXTURE.read_text(encoding="utf-8")))
        documents = await self.collect()
        self.assertEqual([document.url for document in documents], [
            "https://zenodo.org/records/101",
            "https://zenodo.org/records/202",
            "https://zenodo.org/records/606",
        ])
        legacy, invenio, without_type = documents
        self.assertEqual(legacy.doi, "10.5281/zenodo.101")
        self.assertEqual(legacy.language_code, "en")
        self.assertEqual(legacy.raw_meta, {"record_id": "101", "resource_type": "publication-preprint"})
        self.assertEqual(legacy.published_at.date().isoformat(), "2026-05-10")  # type: ignore[union-attr]
        self.assertEqual(invenio.doi, "10.5281/zenodo.202")
        self.assertEqual(invenio.language_code, "ru")
        self.assertEqual(invenio.matched_term, "neuromorphic chips")
        self.assertIsNone(without_type.language_code)
        self.assertIsNone(without_type.doi)
        self.assertEqual(without_type.text, "")

    async def test_malformed_payload_is_parse_error(self) -> None:
        self.http.enqueue({"hits": {"hits": "не список"}})
        with self.assertRaises(AdapterFailure) as error:
            await self.collect()
        self.assertEqual(error.exception.code, AdapterErrorCode.PARSE_ERROR.value)

    async def test_payload_without_hits_is_parse_error(self) -> None:
        self.http.enqueue({"message": "maintenance"})
        with self.assertRaises(AdapterFailure) as error:
            await self.collect()
        self.assertEqual(error.exception.code, AdapterErrorCode.PARSE_ERROR.value)

    def test_language_codes_normalized_to_two_letters(self) -> None:
        self.assertEqual(zenodo._language({"language": "ENG"}), "en")
        self.assertEqual(zenodo._language({"languages": [{"id": "rus"}]}), "ru")
        self.assertEqual(zenodo._language({"language": "de"}), "de")
        self.assertIsNone(zenodo._language({"language": "xyz"}))
        self.assertIsNone(zenodo._language({}))


class RequestTest(ZenodoTestCase):
    """Параметры поиска: только препринты, свежие сверху, фраза целиком."""

    async def test_search_parameters(self) -> None:
        self.http.enqueue(EMPTY)
        await self.collect()
        url, request = self.http.calls[0]
        self.assertEqual(url, zenodo.API_URL)
        self.assertEqual(request["params"], {
            "q": "neuromorphic AND chips",
            "type": "publication",
            "subtype": "preprint",
            "sort": "mostrecent",
            "size": 25,
            "page": 1,
        })
        self.assertEqual(request["headers"], {"Accept": "application/json"})

    def test_all_words_query(self) -> None:
        self.assertEqual(zenodo._all_words_query("AI security in edge-devices"), "AI AND security AND edge AND devices")
        self.assertEqual(
            zenodo._all_words_query('model "cards" (v2): risks/limits'), "model AND cards AND v2 AND risks AND limits"
        )
        self.assertEqual(zenodo._all_words_query("of the"), '"of the"')

    def test_phrase_query_removes_characters_breaking_the_phrase(self) -> None:
        self.assertEqual(zenodo._phrase_query('edge "AI"  \\ chips'), '"edge AI chips"')

    async def test_english_phrases_only_and_at_most_two(self) -> None:
        for _ in range(3):
            self.http.enqueue(EMPTY)
        await self.collect(SearchTerms(ru=("чипы",), en=("first phrase", "second phrase", "third phrase")))
        queries = [request["params"]["q"] for _url, request in self.http.calls]
        self.assertEqual(queries, ["first AND phrase", "second AND phrase"])

    async def test_second_page_only_after_full_page(self) -> None:
        self.http.enqueue({"hits": {"hits": [preprint(n) for n in range(1, 26)]}})
        self.http.enqueue(EMPTY)
        documents = await self.collect()
        self.assertEqual(len(documents), 25)
        self.assertEqual([request["params"]["page"] for _url, request in self.http.calls], [1, 2])

    def test_source_key(self) -> None:
        self.assertIs(self.adapter().key, SourceKey.ZENODO)


class GateTest(ZenodoTestCase):
    """Общий шлюз: интервал, очередь и пауза после отказа."""

    async def assert_cooldown_after(self, failure: Exception, expected_code: str) -> None:
        """Отказ включает паузу: следующий сбор не идёт в сеть и сообщает тот же код."""
        self.http.enqueue(failure)
        with self.assertRaises(AdapterFailure) as first:
            await self.collect()
        self.assertEqual(first.exception.code, expected_code)
        with self.assertRaises(AdapterFailure) as second:
            await self.collect()
        self.assertEqual(second.exception.code, expected_code)
        self.assertIn("пауза", second.exception.message)
        self.assertEqual(len(self.http.calls), 1)

    async def test_cooldown_after_server_error(self) -> None:
        await self.assert_cooldown_after(status_error(503), AdapterErrorCode.HTTP_5XX.value)

    async def test_cooldown_after_timeout(self) -> None:
        await self.assert_cooldown_after(HttpTimeoutError("таймаут"), AdapterErrorCode.TIMEOUT.value)

    async def test_cooldown_after_network_error(self) -> None:
        await self.assert_cooldown_after(HttpTransportError("разрыв"), AdapterErrorCode.HTTP_5XX.value)

    async def test_cooldown_after_rate_limit(self) -> None:
        await self.assert_cooldown_after(status_error(429), AdapterErrorCode.RATE_LIMITED.value)

    async def test_cooldown_after_forbidden(self) -> None:
        await self.assert_cooldown_after(status_error(403), AdapterErrorCode.HTTP_4XX.value)

    async def test_no_cooldown_after_bad_request(self) -> None:
        self.http.enqueue(status_error(400))
        self.http.enqueue(EMPTY)
        with self.assertRaises(AdapterFailure):
            await self.collect()
        await self.collect()
        self.assertEqual(len(self.http.calls), 2)

    async def test_cooldown_expires(self) -> None:
        self.http.enqueue(status_error(503))
        self.http.enqueue(EMPTY)
        with self.assertRaises(AdapterFailure):
            await self.collect()
        self.clock.advance(zenodo.COOLDOWN_SECONDS + 1)
        await self.collect()
        self.assertEqual(len(self.http.calls), 2)

    async def test_interval_between_requests(self) -> None:
        self.http.enqueue(EMPTY)
        self.http.enqueue(EMPTY)
        await self.collect()
        await self.collect()
        self.assertEqual(len(self.clock.slept), 1)
        self.assertAlmostEqual(self.clock.slept[0], zenodo.MIN_GAP_SECONDS, places=6)

    async def test_parallel_collections_share_one_queue(self) -> None:
        self.http.enqueue(EMPTY)
        self.http.enqueue(EMPTY)
        await asyncio.gather(self.collect(), self.collect())
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(len(self.clock.slept), 1)
        self.assertAlmostEqual(self.clock.slept[0], zenodo.MIN_GAP_SECONDS, places=6)


if __name__ == "__main__":
    unittest.main()