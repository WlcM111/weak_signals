"""Регрессионные тесты исправлений аудита: воркер, очередь, очистка HTML, кодировки, Wikipedia."""

from __future__ import annotations

import asyncio
import types
import unittest

from collector.adapters.inbound.worker import CollectionWorker, MaintenanceWorker
from collector.adapters.outbound.sources.base import english_first
from collector.adapters.outbound.sources.xml_utils import parse_xml, text_of
from collector.application.dto import EncyclopediaHit
from collector.application.use_cases.check_encyclopedia import PROBE_CONCURRENCY, CheckEncyclopedia
from collector.domain.rules import decode_body, strip_html
from collector.domain.values import OperationStatus, SearchTerms

from ..fakes import FakeClock, InMemoryEncyclopediaCache


class _Repo:
    """Минимальный репозиторий коллекций: одна коллекция в очереди, журнал завершений."""

    def __init__(self, fail_claims: int = 0, stop: asyncio.Event | None = None) -> None:
        self.finished: list[tuple[str, OperationStatus, str]] = []
        self.claims = 0
        self.sweeps = 0
        self.fail_claims = fail_claims
        self.stop = stop

    async def claim_next(self, owner: str, lease_seconds: int):  # noqa: ANN201
        self.claims += 1
        if self.claims <= self.fail_claims:
            raise ConnectionError("БД недоступна")
        if self.claims == self.fail_claims + 1:
            return types.SimpleNamespace(collection_id="c-1")
        if self.stop is not None:
            self.stop.set()  # очередь пуста: тест завершает цикл без фиксированных пауз
        return None

    async def finish(self, collection_id, owner, status, *, error_code, error_message, finished_at):  # noqa: ANN001, ANN201
        self.finished.append((collection_id, status, error_code))
        return True

    async def release_expired_leases(self) -> int:
        self.sweeps += 1
        if self.sweeps >= 3 and self.stop is not None:
            self.stop.set()
        raise ConnectionError("БД недоступна")


class _Boom:
    async def execute(self, collection, owner):  # noqa: ANN001, ANN201
        raise RuntimeError("сбой записи партии")


class WorkerResilienceTest(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_error_marks_collection_failed(self) -> None:
        repo = _Repo()
        worker = CollectionWorker(repo, _Boom(), owner="w", lease_seconds=60)
        self.assertTrue(await worker.run_once())
        self.assertEqual(repo.finished, [("c-1", OperationStatus.FAILED, "INTERNAL_ERROR")])

    async def test_claim_failure_does_not_kill_worker_loop(self) -> None:
        stop = asyncio.Event()
        repo = _Repo(fail_claims=2, stop=stop)
        worker = CollectionWorker(repo, _Boom(), owner="w", lease_seconds=60, poll_interval=0.001)
        await asyncio.wait_for(worker.run_forever(stop), timeout=5)
        self.assertEqual(len(repo.finished), 1)  # цикл пережил два сбоя БД и дошёл до коллекции

    async def test_maintenance_survives_database_error(self) -> None:
        stop = asyncio.Event()
        repo = _Repo(stop=stop)
        worker = MaintenanceWorker(repo, purge=None, lease_sweep_interval=0.001)
        await asyncio.wait_for(worker.run_forever(stop), timeout=5)
        self.assertGreaterEqual(repo.sweeps, 3)  # три сбоя БД подряд цикл не остановили


class TextCleaningTest(unittest.TestCase):
    def test_strip_html_removes_tags_and_unescapes_entities(self) -> None:
        raw = '<p>Квантовые <a href="https://x.io">сенсоры</a> &amp; ИИ&#8230;</p><script>alert(1)</script>'
        self.assertEqual(" ".join(strip_html(raw).split()), "Квантовые сенсоры & ИИ…")

    def test_strip_html_keeps_math_comparisons(self) -> None:
        self.assertEqual(strip_html("error rate a < b while c > d"), "error rate a < b while c > d")

    def test_windows_1251_feed_is_decoded_and_parsed(self) -> None:
        xml = '<?xml version="1.0" encoding="windows-1251"?><rss><channel><title>Новости ИИ</title></channel></rss>'
        text = decode_body(xml.encode("cp1251"))
        self.assertIn("Новости ИИ", text)
        root = parse_xml(text, what="лента")
        self.assertEqual(text_of(root.find("channel").find("title")), "Новости ИИ")

    def test_utf8_bom_is_removed(self) -> None:
        self.assertEqual(decode_body(b"\xef\xbb\xbf<a>x</a>"), "<a>x</a>")


class EnglishFirstTest(unittest.TestCase):
    def test_english_terms_preferred(self) -> None:
        terms = SearchTerms(ru=("квантовые сенсоры",), en=("quantum sensors", "nv centers"))
        self.assertEqual([term for term, _ in english_first(terms)], ["quantum sensors", "nv centers"])

    def test_russian_terms_used_when_no_english(self) -> None:
        terms = SearchTerms(ru=("квантовые сенсоры",))
        self.assertEqual(english_first(terms), (("квантовые сенсоры", "ru"),))


class _Probe:
    """Проба Wikipedia: считает одновременные вызовы и отказывает на одном названии."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def probe(self, title: str, language_code: str) -> EncyclopediaHit:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            if title == "сбой":
                raise RuntimeError("HTTP 503")
            return EncyclopediaHit(title=title, exists=True, page_url=f"https://ru.wikipedia.org/wiki/{title}")
        finally:
            self.active -= 1


class CheckEncyclopediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_and_failure_tolerant(self) -> None:
        probe = _Probe()
        cache = InMemoryEncyclopediaCache()
        use_case = CheckEncyclopedia(cache, probe, FakeClock(), cache_days=7)
        titles = [f"тема {index}" for index in range(12)] + ["сбой", "тема 0"]
        hits = await use_case.execute(titles, "ru")
        self.assertEqual([hit.title for hit in hits], titles)  # порядок и количество сохранены
        self.assertFalse(hits[12].exists)  # отказ пробы не срывает остальные ответы
        self.assertTrue(hits[13].exists)  # повтор названия берётся из результата первой пробы
        self.assertGreater(probe.peak, 1)
        self.assertLessEqual(probe.peak, PROBE_CONCURRENCY)