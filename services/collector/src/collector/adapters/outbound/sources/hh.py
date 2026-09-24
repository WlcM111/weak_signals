"""Каркас адаптера hh.ru (вакансии).

В каталоге источников `enabled=false` (§6.5 ТЗ, риск R-6): условия использования API не подтверждены.
Поведение при явном запросе источника — код `DISABLED` без выдуманных данных.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from collector.adapters.outbound.sources.base import BaseSourceAdapter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey


class HhAdapter(BaseSourceAdapter):
    """Отключённый источник: реализуется после подтверждения условий API hh.ru."""

    key = SourceKey.HH
    raw_meta_keys = frozenset({"area", "employer"})
    allowed_hosts = frozenset({"api.hh.ru"})

    def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Источник отключён до подтверждения условий доступа."""
        raise AdapterFailure(
            AdapterErrorCode.DISABLED.value, "адаптер hh отключён: условия API не подтверждены"
        )
