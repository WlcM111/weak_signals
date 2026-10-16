"""Этап 5.0 (аудит раунда 3 и round2): понятия вместо форм, текст страниц и повторы, карты сайтов, адреса без отслеживания,
улучшенные версии документов, добор локальных источников, события темы, сущности на русском, расписание."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from collector.adapters.outbound.news_archive.enrich import ArchiveEnricher, page_quality
from collector.adapters.outbound.news_archive.scheduler import ArchiveScheduler
from collector.adapters.outbound.news_archive.sitemaps import SitemapHarvester, parse_sitemap
from collector.adapters.outbound.postgres.archive_repository import concept_groups
from collector.adapters.outbound.sources.news_archive import NewsArchiveAdapter
from collector.application.ports import HttpTimeoutError
from collector.domain.archive import ArchiveEntry
from collector.domain.event_channel import extract_entities
from collector.domain.normalization import text_quality_of
from collector.domain.technology_filters import is_mature
from collector.domain.technology_mining import candidate_phrases
from ws_common.identity import without_tracking

from ..fakes import FakeClock, FakeHttpClient, FakeRateLimiter

SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
 xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://example.com/news/2026/09/tetragen-launches-autonomous-welding-cell</loc><lastmod>2026-09-20</lastmod>
<news:news><news:publication_date>2026-09-19</news:publication_date><news:title>Tetragen launches welding cell</news:title></news:news></url>
<url><loc>https://example.com/about</loc><lastmod>2026-09-20</lastmod></url>
<url><loc>https://example.com/news/2024/01/old-robot-arm-update-story</loc><lastmod>2024-01-02</lastmod></url>
</urlset>"""


class ConceptsAndTextTest(unittest.TestCase):
    def test_forms_are_one_concept(self) -> None:
        groups = concept_groups(["robot", "robots", "transformer", "transformers"])
        self.assertEqual(sorted(sorted(g) for g in groups), [["robot", "robots"], ["transformer", "transformers"]])

    def test_page_quality_and_text_quality(self) -> None:
        self.assertEqual(page_quality("<p>Please verify you are a human</p>", ""), "blocked")
        self.assertEqual(page_quality("", "Please verify that you are a human before continuing."), "blocked")
        self.assertEqual(page_quality("", "Tetragen raised CAD 1.8 million to expand autonomous welding cells."), "ok")
        self.assertEqual(page_quality("", ""), "empty")
        self.assertEqual(text_quality_of("Title", "Title"), "title")
        self.assertEqual(text_quality_of("Title", "x" * 400), "abstract")
        self.assertEqual(text_quality_of("Title", "x" * 2000), "article")

    def test_tracking_removed_identity_kept(self) -> None:
        self.assertEqual(without_tracking("https://blogs.cisco.com/?p=495626&utm_source=x#top"), "https://blogs.cisco.com/?p=495626")
        self.assertNotEqual(without_tracking("https://e.com/article?id=101"), without_tracking("https://e.com/article?id=102"))

    def test_acronyms_entities_and_industrial_applications(self) -> None:
        self.assertIn("ocs", candidate_phrases("OCS fabric ships for AI clusters"))
        self.assertNotIn("llm", candidate_phrases("LLM serving ships"))
        self.assertIn("РобоТех", extract_entities(["РобоТех представила сварочного робота"]))
        self.assertFalse(is_mature("federated learning for distributed factories"))  # pos-0082
        self.assertTrue(is_mature("federated learning for systems"))


class SitemapTest(unittest.IsolatedAsyncioTestCase):
    def test_parse(self) -> None:
        pages, children = parse_sitemap(SITEMAP)
        self.assertEqual((len(pages), children), (3, []))
        self.assertEqual(pages[0][2:], ("Tetragen launches welding cell", date(2026, 9, 19)))

    async def test_fresh_article_pages_become_archive_entries(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.entries = []

            async def save_entries(self, entries):  # noqa: ANN001, ANN201
                self.entries += list(entries)
                return len(entries)

        class Http:
            async def get_text(self, url, allowed_hosts, params=None, headers=None):  # noqa: ANN001, ANN201
                if url.endswith("/robots.txt"):
                    return "Sitemap: https://example.com/news-sitemap.xml"
                if url.endswith("/news-sitemap.xml"):
                    return SITEMAP
                raise HttpTimeoutError("нет")

        from collector.adapters.outbound.news_archive.feeds import RegistrySource

        clock = FakeClock(datetime(2026, 10, 12, tzinfo=UTC))
        store = Store()
        found, saved = await SitemapHarvester(Http(), store, clock, 0.0, report=lambda _l: None).run(
            [RegistrySource("D001", "https://example.com/")])
        self.assertEqual((found, saved), (1, 1))  # /about — не статья, 2024 — вне окна
        entry = store.entries[0]
        self.assertEqual((entry.origin, entry.title, entry.published_on, entry.published_source),
                         ("sitemap", "Tetragen launches welding cell", date(2026, 9, 19), "sitemap"))


class EnrichRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_retry_not_failed(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.saved = []

            async def entries_to_enrich(self, cues, limit):  # noqa: ANN001, ANN201
                return [ArchiveEntry("https://techcrunch.com/2026/08/11/x/", "techcrunch.com", "wayback", "X", date(2026, 8, 11),
                                     enrich_attempts=1)]

            async def save_enrichment(self, url, title, summary, published_on, text_status, tokens, retry_minutes=0):  # noqa: ANN001, ANN201
                self.saved.append((text_status, retry_minutes))

        class Http:
            async def get_text(self, url, allowed_hosts, params=None, headers=None):  # noqa: ANN001, ANN201
                raise HttpTimeoutError("таймаут")

        store = Store()
        await ArchiveEnricher(Http(), store, FakeClock(), ("raises",), 0.0, report=lambda _l: None).run(5)
        self.assertEqual(store.saved, [("retry", 60)])  # 30 мин × 2^1


class RefillAndSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_news_archive_skips_collected_and_refills(self) -> None:
        class Store:
            async def search(self, words, since, limit, min_match):  # noqa: ANN001, ANN201
                return [ArchiveEntry(f"https://e.com/{i}", "e.com", "wayback", f"Robot skill story {i}", date(2026, 9, 1))
                        for i in range(limit)]

        adapter = NewsArchiveAdapter(FakeHttpClient(), FakeRateLimiter(), FakeClock(), Store())
        found = [d.url async for d in adapter.search_targeted(["robot skill"], 2, date(2024, 1, 1), 10**9,
                                                               exclude=frozenset({"https://e.com/0"}))]
        self.assertEqual(found, ["https://e.com/1", "https://e.com/2"])

    async def test_scheduler_runs_due_jobs_once(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.runs = {}

            async def last_job_run(self, job):  # noqa: ANN001, ANN201
                return self.runs.get(job)

            async def mark_job_run(self, job, status, detail):  # noqa: ANN001, ANN201
                self.runs[job] = clock.now()

        class Log:
            def info(self, *a, **k) -> None:  # noqa: ANN002, ANN003
                return None

            warning = info

        clock = FakeClock(datetime(2026, 10, 12, tzinfo=UTC))
        calls = []

        async def runner(job: str) -> str:
            calls.append(job)
            return "ok"

        scheduler = ArchiveScheduler(runner, {"rss": 24.0, "wayback": 0.0}, Store(), clock, Log())
        self.assertEqual(await scheduler.tick(), ["rss"])
        self.assertEqual(await scheduler.tick(), [])  # срок ещё не наступил
        self.assertEqual(calls, ["rss"])


class Stage501Test(unittest.TestCase):
    def test_entities_without_city_prefix_and_incumbents(self) -> None:
        entities = extract_entities(["London’s Sava raises seed", "AWS launches new region", "Keycard raises $38M"])
        self.assertEqual(sorted(entities), ["Keycard", "Sava"])


if __name__ == "__main__":
    unittest.main()


class Stage51CollectorTest(unittest.TestCase):
    def test_low_quality_domains_are_low_trust_and_gdelt_override_parses(self) -> None:
        from collector.config_parsing import parse_source_override
        from collector.domain.classify import ClassificationConfig, classify
        from collector.domain.values import SourceKey, SourceType, TrustLevel

        config = ClassificationConfig(
            source_defaults={SourceKey.CROSSREF: (SourceType.SCIENTIFIC_PUBLICATION, TrustLevel.HIGH)},
            type_trust={SourceType.SCIENTIFIC_PUBLICATION: TrustLevel.HIGH}, low_quality_domains=("wjftcse.org",))
        _, trust = classify(config, SourceKey.CROSSREF, "wjftcse.org", "Self-reconfiguring robots", has_published_at=True)
        self.assertIs(trust, TrustLevel.LOW)
        _, normal = classify(config, SourceKey.CROSSREF, "nature.com", "Robots", has_published_at=True)
        self.assertIs(normal, TrustLevel.HIGH)
        self.assertEqual(parse_source_override("gdelt=false"), {SourceKey.GDELT: False})
