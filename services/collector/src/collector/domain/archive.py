"""Записи локальных архивов (этап 4.3): статьи изданий и записи RSS, работы корпуса arXiv."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class ArchiveEntry:
    """Статья издания: из Wayback Machine (заголовок — слова адреса) или из RSS (настоящий заголовок и анонс)."""

    url: str
    publisher: str
    origin: str  # "wayback" | "rss"
    title: str
    published_on: date | None
    captured_at: datetime | None = None
    summary: str = ""
    tokens: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CorpusPaper:
    """Работа корпуса arXiv: заголовок, начало аннотации, дата первой версии, разделы."""

    paper_id: str
    title: str
    created_on: date
    abstract: str = ""
    categories: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorpusHarvestState:
    """Состояние выгрузки набора OAI-PMH: токен продолжения и счётчики."""

    set_spec: str
    resumption_token: str | None
    records: int
    kept: int
    status: str
