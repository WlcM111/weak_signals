"""Трасса одного сбора: сырые ответы источников и судьба каждого найденного документа (аудит A3, этап 2).

Запись живёт в контекстной переменной, как учёт запросов (`request_accounting`): сценарий сбора открывает трассу
перед запуском адаптеров, задачи адаптеров наследуют контекст. Параллельные сборы получают разные объекты и не
смешивают записи. Из адресов и тел запросов до записи удаляются секреты (ключи API, токены); заголовки запросов,
в которых ключи передаются чаще всего, не записываются вовсе.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_MARKERS = ("key", "token", "secret", "password", "passwd", "auth", "signature", "credential")
REDACTED = "***"
MAX_DETAIL = 500
MAX_ERROR = 500


class DocumentEvent(StrEnum):
    """Судьба документа, выданного адаптером."""

    ACCEPTED = "ACCEPTED"  # принят в коллекцию
    DEDUP = "DEDUP"  # дубль уже принятого документа (URL, содержимое или DOI)
    SOURCE_CAP = "SOURCE_CAP"  # превышен лимит документов на источник
    REJECTED = "REJECTED"  # отбракован нормализацией (реклама, пустой текст, нарушение инварианта)


def is_secret_name(name: str) -> bool:
    """Похоже ли имя параметра на секрет (ключ API, токен, пароль)."""
    lowered = name.lower()
    return any(marker in lowered for marker in SECRET_MARKERS)


def redact_url(url: str, params: Mapping[str, Any] | None = None) -> str:
    """Адрес запроса вместе с параметрами, но без секретов и без учётных данных в адресе."""
    parts = urlsplit(url)
    query = [(key, REDACTED if is_secret_name(key) else value)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    query += [(str(key), REDACTED if is_secret_name(str(key)) else str(value)) for key, value in (params or {}).items()]
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), ""))


def redact_body(body: Any) -> Any:
    """Тело запроса без значений секретных полей (рекурсивно)."""
    if isinstance(body, Mapping):
        return {str(key): (REDACTED if is_secret_name(str(key)) else redact_body(value)) for key, value in body.items()}
    if isinstance(body, list | tuple):
        return [redact_body(value) for value in body]
    return body


def payload_text(payload: Any, as_json: bool) -> str:
    """Ответ источника в текстовом виде: JSON — сериализация разобранного ответа, XML/RSS — текст как есть."""
    return json.dumps(payload, ensure_ascii=False) if as_json else str(payload)


@dataclass(frozen=True, slots=True)
class ResponseRecord:
    """Один логический запрос адаптера: адрес без секретов, итог и сжатое тело ответа."""

    seq: int
    source_key: str
    method: str
    url: str
    request_body: Any
    outcome: str  # OK или код отказа AdapterErrorCode (RATE_LIMITED, TIMEOUT, …), CANCELLED, ERROR
    error_message: str | None
    body_gzip: bytes | None
    body_sha256: str | None
    body_bytes: int
    started_at: datetime
    duration_ms: int


@dataclass(frozen=True, slots=True)
class EventRecord:
    """Событие документа: по какой фразе и каким источником найден, что с ним стало."""

    seq: int
    source_key: str
    matched_term: str
    event: DocumentEvent
    url: str
    title: str
    url_hash: str | None = None
    doi: str | None = None
    detail: str | None = None


@dataclass
class CollectionTrace:
    """Накопитель трассы одного выполнения сбора; записи сбрасываются в хранилище партиями."""

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    responses: list[ResponseRecord] = field(default_factory=list)
    events: list[EventRecord] = field(default_factory=list)
    response_seq: int = 0
    event_seq: int = 0
    save_failures: int = 0

    def add_response(
        self,
        *,
        source_key: str,
        method: str,
        url: str,
        params: Mapping[str, Any] | None,
        json_body: Mapping[str, Any] | None,
        outcome: str,
        error_message: str | None,
        payload: Any,
        as_json: bool,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        """Записывает итог запроса; тело хранится сжатым, с SHA-256 исходного текста."""
        self.response_seq += 1
        raw = payload_text(payload, as_json).encode("utf-8") if outcome == "OK" else None
        self.responses.append(
            ResponseRecord(
                seq=self.response_seq,
                source_key=source_key,
                method=method,
                url=redact_url(url, params),
                request_body=redact_body(dict(json_body)) if json_body is not None else None,
                outcome=outcome,
                error_message=(error_message or "")[:MAX_ERROR] or None,
                body_gzip=gzip.compress(raw, mtime=0) if raw is not None else None,
                body_sha256=hashlib.sha256(raw).hexdigest() if raw is not None else None,
                body_bytes=len(raw) if raw is not None else 0,
                started_at=started_at,
                duration_ms=max(0, int(duration_ms)),
            )
        )

    def add_event(
        self,
        *,
        source_key: str,
        matched_term: str,
        event: DocumentEvent,
        url: str,
        title: str,
        url_hash: str | None = None,
        doi: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Записывает событие документа."""
        self.event_seq += 1
        self.events.append(
            EventRecord(
                seq=self.event_seq,
                source_key=source_key,
                matched_term=matched_term[:500],
                event=event,
                url=redact_url(url) if url else "",
                title=(title or "")[:512],
                url_hash=url_hash,
                doi=doi,
                detail=(detail or "")[:MAX_DETAIL] or None,
            )
        )

    def drain(self) -> tuple[list[ResponseRecord], list[EventRecord]]:
        """Забирает накопленные записи для сохранения."""
        responses, events = self.responses, self.events
        self.responses, self.events = [], []
        return responses, events


_CURRENT: ContextVar[CollectionTrace | None] = ContextVar("collector_collection_trace", default=None)


def begin_collection_trace() -> CollectionTrace:
    """Открывает трассу для текущего контекста и задач, которые будут созданы из него."""
    trace = CollectionTrace()
    _CURRENT.set(trace)
    return trace


def current_collection_trace() -> CollectionTrace | None:
    """Трасса текущего сбора; None вне сбора или при выключенной трассировке."""
    return _CURRENT.get()
