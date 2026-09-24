"""Часы как порт: домен и use cases не обращаются к datetime.now напрямую (детерминируемость тестов)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Источник текущего времени и асинхронных пауз."""

    def now(self) -> datetime:
        """Текущее время в UTC."""

    def monotonic(self) -> float:
        """Монотонные секунды для измерения длительностей и token bucket."""

    async def sleep(self, seconds: float) -> None:
        """Асинхронная пауза."""


class SystemClock:
    """Реальные системные часы."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return asyncio.get_event_loop().time()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class SyncClock(Protocol):
    """Синхронный источник времени для сервисов на sync-стеке (ADR-09: analyzer)."""

    def now(self) -> datetime:
        """Текущее время в UTC."""

    def monotonic(self) -> float:
        """Монотонные секунды для измерения длительностей и аренд."""

    def sleep(self, seconds: float) -> None:
        """Пауза текущего потока."""


class SyncSystemClock:
    """Реальные системные часы без event loop."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)
