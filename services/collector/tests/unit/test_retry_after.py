"""Retry-After в обеих формах и отсутствие скрытых длинных повторов в HTTP-клиенте."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from collector.domain.retry_after import parse_retry_after

HTTPX = importlib.util.find_spec("httpx") is not None


class ParseRetryAfterTest(unittest.TestCase):
    def test_delta_seconds(self) -> None:
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertEqual(parse_retry_after(" 0 "), 0.0)

    def test_http_date(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        value = format_datetime(now + timedelta(seconds=30), usegmt=True)
        self.assertAlmostEqual(parse_retry_after(value, now), 30.0, places=3)

    def test_past_date_and_garbage(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
        self.assertEqual(parse_retry_after(format_datetime(now - timedelta(minutes=5), usegmt=True), now), 0.0)
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after(None))


@unittest.skipUnless(HTTPX, "httpx не установлен в этой среде")
class NoHiddenLongRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_long_retry_after_is_not_retried_inside_client(self) -> None:
        from collector.adapters.outbound.http_client import HttpxClient  # noqa: PLC0415
        from collector.application.ports import HttpStatusError  # noqa: PLC0415

        client = HttpxClient("test@example.org", 5.0, 1_000_000, retries=3)
        calls: list[int] = []

        async def attempt(*_: object) -> bytes:
            calls.append(1)
            raise HttpStatusError(429, "источник ответил кодом 429", 60.0)

        client._attempt = attempt  # type: ignore[method-assign]  # noqa: SLF001
        with self.assertRaises(HttpStatusError):
            await client._get("https://api.openalex.org/works", allowed_hosts=frozenset({"openalex.org"}),  # noqa: SLF001
                              params=None, headers=None)
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.attempts_total, 1)
        await client.aclose()
