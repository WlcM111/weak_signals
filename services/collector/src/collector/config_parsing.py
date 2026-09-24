"""Разбор составных значений переменных окружения collector (без зависимости от pydantic)."""

from __future__ import annotations

from urllib.parse import urlsplit

from collector.domain.values import SourceKey

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def parse_feed_list(value: str) -> tuple[str, ...]:
    """`WS_COLLECTOR_RSS_FEEDS`: URL лент через запятую; ValueError при непригодном URL."""
    feeds = tuple(item.strip() for item in value.split(",") if item.strip())
    if not feeds:
        raise ValueError("требуется хотя бы одна лента RSS")
    for feed in feeds:
        parts = urlsplit(feed)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"недопустимый URL ленты: {feed}")
    return feeds


def parse_source_override(value: str) -> dict[SourceKey, bool]:
    """`WS_SOURCE_ENABLED_OVERRIDE`: `openalex=true,hh=false`; ValueError при неизвестном источнике."""
    override: dict[SourceKey, bool] = {}
    for chunk in (item.strip() for item in value.split(",") if item.strip()):
        key, separator, flag = chunk.partition("=")
        if not separator:
            raise ValueError(f"ожидается формат <источник>=<true|false>, получено: {chunk}")
        normalized = flag.strip().lower()
        if normalized not in _TRUE_VALUES | _FALSE_VALUES:
            raise ValueError(f"недопустимое значение флага: {flag}")
        try:
            source_key = SourceKey(key.strip().lower())
        except ValueError as exc:
            raise ValueError(f"неизвестный источник: {key}") from exc
        override[source_key] = normalized in _TRUE_VALUES
    return override
