"""Дедупликация документов в пределах одного сбора (§9.2 ТЗ).

Уровни: (1) точный URL — `url_hash`; (2) DOI; (3) `content_hash`. Кросс-коллекционная дедупликация
выполняется хранилищем (`UNIQUE url_hash` + поиск по DOI); здесь исключаются повторы внутри партии.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class DedupReason(StrEnum):
    """Причина, по которой документ признан повтором."""

    URL = "url"
    DOI = "doi"
    CONTENT = "content"


@dataclass(slots=True)
class DedupIndex:
    """Множества ключей, уже встреченных в текущем сборе."""

    url_hashes: set[str] = field(default_factory=set)
    dois: set[str] = field(default_factory=set)
    content_hashes: set[str] = field(default_factory=set)

    def check(self, url_hash: str, content_hash: str, doi: str | None) -> DedupReason | None:
        """Возвращает причину дубликата или None, если документ новый для этого сбора."""
        if url_hash in self.url_hashes:
            return DedupReason.URL
        if doi and doi in self.dois:
            return DedupReason.DOI
        if content_hash in self.content_hashes:
            return DedupReason.CONTENT
        return None

    def add(self, url_hash: str, content_hash: str, doi: str | None) -> None:
        """Регистрирует ключи документа."""
        self.url_hashes.add(url_hash)
        self.content_hashes.add(content_hash)
        if doi:
            self.dois.add(doi)

    def register_if_new(self, url_hash: str, content_hash: str, doi: str | None) -> DedupReason | None:
        """Проверяет и сразу регистрирует новый документ (один проход вместо check + add)."""
        reason = self.check(url_hash, content_hash, doi)
        if reason is None:
            self.add(url_hash, content_hash, doi)
        return reason

    @property
    def size(self) -> int:
        """Число уникальных документов, прошедших дедупликацию."""
        return len(self.url_hashes)
