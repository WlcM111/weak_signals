"""Этап 4.5: канал «события стартапов» из архива новостей, отсев шаблонов новостей, бюджет Hacker News."""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from collector.adapters.outbound.rules_loader import load_classification_config
from collector.application.dto import CollectionDraft
from collector.application.use_cases.run_collection import RunCollection, RunCollectionConfig
from collector.domain.archive import ArchiveEntry
from collector.domain.event_channel import EVENT_CUES, event_vocabulary, is_news_boilerplate, rank_events
from collector.domain.source_policy import DEFAULT_POLICIES
from collector.domain.values import CollectionLimits, CollectionMode, OperationStatus, SearchTerms, SourceKey

from ..fakes import FakeClock, InMemoryAdapterRunRepository, InMemoryCollectionRepository, InMemoryDocumentRepository, raw_document
from .test_targeted_search import TargetedScriptedAdapter


def entry(slug: str, day: date) -> ArchiveEntry:
    return ArchiveEntry(f"https://techcrunch.com/{day:%Y/%m/%d}/{slug}/", "techcrunch.com", "wayback",
                        slug.replace("-", " ").capitalize(), day, tokens=tuple(slug.split("-")))


class EventChannelDomainTest(unittest.TestCase):
    def test_vocabulary_from_topic_and_selected_technologies(self) -> None:
        vocabulary = event_vocabulary(["industrial AI manufacturing"], ["autonomous welding robots", "AI data"])
        for word in ("industrial", "manufacturing", "autonomous", "welding", "robots", "robot"):
            self.assertIn(word, vocabulary)
        self.assertNotIn("ai", vocabulary)
        self.assertNotIn("data", vocabulary)

    def test_rare_matched_words_rank_first(self) -> None:
        common = [(entry(f"robot-startup-raises-round-{i}", date(2026, 5, 1)), f"robot startup raises round {i}".split())
                  for i in range(6)]
        special = entry("tetragen-raises-seed-to-expand-autonomous-welding-robots", date(2026, 8, 11))
        candidates = [*common, (special, special.tokens)]
        ranked = rank_events(candidates, ["robots", "robot", "autonomous", "welding"], min_overlap=2, limit=3)
        self.assertEqual(ranked[0][0].url, special.url)  # «welding» и «autonomous» редки в выборке
        self.assertEqual(ranked[0][1][:2], ["autonomous", "welding"])
        self.assertEqual(len(ranked), 1)  # у остальных совпало одно слово (robot) — ниже порога 2

    def test_news_boilerplate(self) -> None:
        self.assertTrue(is_news_boilerplate("germany semiconductor memory market"))
        self.assertTrue(is_news_boilerplate("data center colocation databook"))
        self.assertFalse(is_news_boilerplate("agentic payments"))
        self.assertIn("raises", EVENT_CUES)

    def test_hackernews_budget_covers_broad_targeted_and_second_round(self) -> None:
        self.assertEqual(DEFAULT_POLICIES[SourceKey.HACKERNEWS].max_requests, 80)


class _Archive:
    def __init__(self, candidates: list[tuple[ArchiveEntry, tuple[str, ...]]]) -> None:
        self.candidates, self.calls = candidates, []

    async def event_candidates(self, vocabulary, cues, min_overlap, since, limit):  # noqa: ANN001, ANN201
        self.calls.append((tuple(vocabulary), min_overlap, limit))
        return self.candidates

    async def titles_matching(self, keywords, min_overlap, start, end, limit, origin=None):  # noqa: ANN001, ANN201
        self.calls.append(("titles", origin))
        return []


class EventChannelInCollectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_archive_targeted_pass_starts_with_event_channel(self) -> None:
        clock = FakeClock()
        runs = InMemoryAdapterRunRepository()
        collections = InMemoryCollectionRepository(clock, runs)
        documents = InMemoryDocumentRepository(clock, collections)
        special = entry("tetragen-raises-seed-to-expand-autonomous-welding-robots", date(2026, 8, 11))
        filler = [(entry(f"robot-maker-raises-round-{i}", date(2026, 5, 1)), tuple(f"robot maker raises round {i}".split()))
                  for i in range(5)]
        archive = _Archive([(special, special.tokens), *filler])
        adapter = TargetedScriptedAdapter(SourceKey.NEWS_ARCHIVE, [raw_document(f"https://example.org/{i}", title=f"Item {i}")
                                                                    for i in range(3)])
        config_dir = Path(__file__).resolve().parents[2] / "config"
        collection, _ = await collections.create_if_absent(CollectionDraft(
            idempotency_key="job-45:collect", query_text="промышленный ИИ", mode=CollectionMode.SEARCH,
            terms=SearchTerms(ru=("промышленный ИИ",), en=("industrial AI manufacturing",),
                              technologies=("autonomous welding robots",)),
            limits=CollectionLimits.defaults(CollectionMode.SEARCH), sources=(SourceKey.NEWS_ARCHIVE,)))
        collection.transition_to(OperationStatus.RUNNING, clock.now())
        use_case = RunCollection(
            collections=collections, documents=documents, adapter_runs=runs, adapters={SourceKey.NEWS_ARCHIVE: adapter},
            classification=load_classification_config(config_dir / "trust_rules.yaml", config_dir / "rss_domains.yaml"),
            clock=clock, config=RunCollectionConfig(lease_seconds=60, heartbeat_seconds=10_000, idle_poll_seconds=0.01),
            archive=archive,
        )
        await use_case.execute(collection, "collector:test")
        stored = {doc.url: doc for doc in documents.documents.values()}
        self.assertIn(special.url, stored)  # статья канала событий попала в сбор
        self.assertEqual(adapter.targeted_calls[0][0], "autonomous welding robots")  # затем — поиск по технологиям
        self.assertIn(("titles", "rss"), archive.calls)  # оценка возникновения по новостям — только заголовки RSS


if __name__ == "__main__":
    unittest.main()
