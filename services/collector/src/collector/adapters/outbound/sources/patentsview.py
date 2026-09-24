"""Каркас адаптера PatentsView (патенты USPTO).

В каталоге источников `enabled=false` (§6.5 ТЗ, риск R-6): условия доступа и формат запроса не
подтверждены. Класс существует, чтобы включение источника не требовало изменения композиции:
при явном запросе источника запуск завершится кодом `DISABLED`, а не выдумает данные.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from collector.adapters.outbound.sources.base import BaseSourceAdapter
from collector.domain.entities import RawDocument
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode, CollectionLimits, SearchTerms, SourceKey


class PatentsViewAdapter(BaseSourceAdapter):
    """Отключённый источник: реализуется после получения ключа и подтверждения условий API."""

    key = SourceKey.PATENTSVIEW
    raw_meta_keys = frozenset({"patent_type"})
    allowed_hosts = frozenset({"search.patentsview.org"})

    def search(
        self, terms: SearchTerms, limits: CollectionLimits, deadline: float
    ) -> AsyncIterator[RawDocument]:
        """Источник отключён до подтверждения условий доступа."""
        raise AdapterFailure(
            AdapterErrorCode.DISABLED.value,
            "адаптер patentsview отключён: условия API и ключ не подтверждены",
        )
