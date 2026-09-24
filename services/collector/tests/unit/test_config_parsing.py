"""Разбор составных переменных окружения collector."""

from __future__ import annotations

import unittest

from collector.config_parsing import parse_feed_list, parse_source_override
from collector.domain.values import SourceKey


class FeedListTest(unittest.TestCase):
    """`WS_COLLECTOR_RSS_FEEDS`."""

    def test_parses_and_trims(self) -> None:
        feeds = parse_feed_list(" https://a.example/feed , https://b.example/rss ")
        self.assertEqual(feeds, ("https://a.example/feed", "https://b.example/rss"))

    def test_rejects_empty_and_invalid(self) -> None:
        for value in ("", " , ", "ftp://a.example/feed", "нет-схемы"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_feed_list(value)


class SourceOverrideTest(unittest.TestCase):
    """`WS_SOURCE_ENABLED_OVERRIDE`."""

    def test_parses_flags(self) -> None:
        override = parse_source_override("openalex=true,hh=false, github = 1 ")
        self.assertEqual(
            override, {SourceKey.OPENALEX: True, SourceKey.HH: False, SourceKey.GITHUB: True}
        )

    def test_rejects_invalid(self) -> None:
        for value in ("openalex", "openalex=maybe", "unknown=true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_source_override(value)


if __name__ == "__main__":
    unittest.main()
