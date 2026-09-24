"""Сценарий CheckEncyclopedia: индикатор зрелости технологии по Wikipedia с кешем."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta

from collector.application.dto import EncyclopediaHit
from collector.application.ports import EncyclopediaCacheRepository, EncyclopediaProbe
from collector.domain.rules import normalize_encyclopedia_title
from ws_common.clock import Clock
from ws_common.logging import get_logger

PROBE_CONCURRENCY = 5


class CheckEncyclopedia:
    """Проверяет наличие статей; свежие результаты берутся из кеша, остальные запрашиваются у Wikipedia."""

    def __init__(
        self,
        cache: EncyclopediaCacheRepository,
        probe: EncyclopediaProbe,
        clock: Clock,
        cache_days: int,
    ) -> None:
        self._cache = cache
        self._probe = probe
        self._clock = clock
        self._cache_days = cache_days
        self._log = get_logger("collector.check_encyclopedia")

    async def execute(self, titles: Sequence[str], language_code: str) -> tuple[EncyclopediaHit, ...]:
        """Возвращает по одному результату на каждое название в порядке запроса."""
        fresh_after = self._clock.now() - timedelta(days=self._cache_days)
        normalized = [normalize_encyclopedia_title(title) for title in titles]
        cached = await self._cache.get_many(language_code, sorted(set(normalized)), fresh_after)
        first_title = {}
        for title, title_norm in zip(titles, normalized, strict=True):
            first_title.setdefault(title_norm, title)
        missing = [title_norm for title_norm in first_title if title_norm not in cached]
        misses = len(missing)
        semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def probe(title_norm: str) -> tuple[str, EncyclopediaHit | None]:
            """Одна проверка; отказ Wikipedia по названию не срывает ответ по остальным."""
            try:
                async with semaphore:
                    hit = await self._probe.probe(first_title[title_norm], language_code)
                await self._cache.put(language_code, title_norm, hit)
            except Exception as error:  # noqa: BLE001 - индикатор зрелости необязателен
                self._log.warning("encyclopedia.probe_failed", title=title_norm[:80], error=str(error)[:200])
                return title_norm, None
            return title_norm, hit

        for title_norm, probed in await asyncio.gather(*(probe(item) for item in missing)):
            if probed is not None:
                cached[title_norm] = probed
        results: list[EncyclopediaHit] = []
        for title, title_norm in zip(titles, normalized, strict=True):
            hit = cached.get(title_norm) or EncyclopediaHit(title=title, exists=False)
            results.append(
                EncyclopediaHit(
                    title=title,
                    exists=hit.exists,
                    page_url=hit.page_url,
                    pageviews_30d=hit.pageviews_30d,
                    created_at=hit.created_at,
                )
            )
        self._log.info(
            "encyclopedia.check",
            titles=len(titles),
            hits=sum(1 for hit in results if hit.exists),
            cache_hits=len(titles) - misses,
            language=language_code,
        )
        return tuple(results)
