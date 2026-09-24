"""Сущности домена collector: коллекция, документ, запуск адаптера (§9.2 ТЗ)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from collector.domain.errors import InvariantViolation
from collector.domain.rules import (
    MAX_ORIGIN_DOMAIN_LENGTH,
    MAX_TEXT_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_URL_LENGTH,
    UNKNOWN_LANGUAGE,
)
from collector.domain.values import (
    AdapterErrorCode,
    CollectionErrorCode,
    CollectionLimits,
    CollectionMode,
    Lease,
    OperationStatus,
    SearchTerms,
    SourceKey,
    SourceType,
    TrustLevel,
)

_ALLOWED_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PENDING: frozenset({OperationStatus.RUNNING, OperationStatus.CANCELLED}),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.COMPLETED,
            OperationStatus.PARTIAL,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
            OperationStatus.PENDING,  # возврат в очередь при потере аренды
        }
    ),
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.PARTIAL: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class RawDocument:
    """Документ в виде, полученном от адаптера источника (до нормализации и классификации)."""

    url: str
    title: str
    text: str
    matched_term: str
    published_at: datetime | None = None
    language_code: str | None = None
    doi: str | None = None
    citation_count: int | None = None
    engagement_count: int | None = None
    raw_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentDraft:
    """Нормализованный документ, готовый к записи (идентификатор присваивает PostgreSQL)."""

    url: str
    canonical_url: str
    url_hash: str
    origin_domain: str
    title: str
    text: str
    content_hash: str
    language_code: str
    source_key: SourceKey
    source_type: SourceType
    trust_level: TrustLevel
    fetched_at: datetime
    matched_term: str
    published_at: datetime | None = None
    doi: str | None = None
    citation_count: int | None = None
    engagement_count: int | None = None
    raw_meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_document_invariants(
            url=self.url,
            title=self.title,
            text=self.text,
            language_code=self.language_code,
            origin_domain=self.origin_domain,
            citation_count=self.citation_count,
            engagement_count=self.engagement_count,
        )


@dataclass(frozen=True, slots=True)
class Document:
    """Документ корпуса, отдаваемый наружу (`weaksignals.common.v1.Document`)."""

    document_id: str
    url: str
    title: str
    text: str
    language_code: str
    source_key: SourceKey
    source_type: SourceType
    trust_level: TrustLevel
    fetched_at: datetime
    origin_domain: str
    published_at: datetime | None = None
    doi: str | None = None
    citation_count: int | None = None
    engagement_count: int | None = None

    def __post_init__(self) -> None:
        if len(self.document_id) != 36:
            raise InvariantViolation("Document.document_id должен быть UUID (36 символов)")
        _check_document_invariants(
            url=self.url,
            title=self.title,
            text=self.text,
            language_code=self.language_code,
            origin_domain=self.origin_domain,
            citation_count=self.citation_count,
            engagement_count=self.engagement_count,
        )


def _check_document_invariants(
    *,
    url: str,
    title: str,
    text: str,
    language_code: str,
    origin_domain: str,
    citation_count: int | None,
    engagement_count: int | None,
) -> None:
    """Проверяет инварианты документа, общие для черновика и сохранённой записи (§10.1)."""
    if not url.startswith(("http://", "https://")) or len(url) > MAX_URL_LENGTH:
        raise InvariantViolation("Document.url: требуется абсолютный http(s) URL ≤ 2048 символов")
    if not 1 <= len(title) <= MAX_TITLE_LENGTH:
        raise InvariantViolation("Document.title: длина 1..512")
    if len(text) > MAX_TEXT_LENGTH:
        raise InvariantViolation("Document.text: длина ≤ 8000")
    if language_code != UNKNOWN_LANGUAGE and not 2 <= len(language_code) <= 3:
        raise InvariantViolation("Document.language_code: ISO 639-1 или 'und'")
    if not origin_domain or len(origin_domain) > MAX_ORIGIN_DOMAIN_LENGTH:
        raise InvariantViolation("Document.origin_domain: непустой хост ≤ 255 символов")
    if citation_count is not None and citation_count < 0:
        raise InvariantViolation("Document.citation_count ≥ 0")
    if engagement_count is not None and engagement_count < 0:
        raise InvariantViolation("Document.engagement_count ≥ 0")


@dataclass(slots=True)
class AdapterRun:
    """Запуск одного адаптера в рамках коллекции (таблица `adapter_runs`)."""

    source_key: SourceKey
    status: OperationStatus = OperationStatus.PENDING
    http_requests: int = 0
    documents_found: int = 0
    documents_new: int = 0
    error_code: str = ""
    error_message: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def start(self, at: datetime) -> None:
        """Переводит запуск в RUNNING."""
        self.status = OperationStatus.RUNNING
        self.started_at = at

    def complete(self, at: datetime, note: AdapterErrorCode | None = None) -> None:
        """Успешное завершение; `note` фиксирует неошибочную причину остановки (BUDGET_EXHAUSTED)."""
        self.status = OperationStatus.COMPLETED
        self.error_code = note.value if note else ""
        self.finished_at = at

    def fail(self, at: datetime, code: AdapterErrorCode, message: str) -> None:
        """Отказ адаптера: сохраняется код и усечённое сообщение (≤ 500 символов по DDL)."""
        self.status = OperationStatus.FAILED
        self.error_code = code.value
        self.error_message = message[:500]
        self.finished_at = at

    def cancel(self, at: datetime) -> None:
        """Отмена по требованию пользователя."""
        self.status = OperationStatus.CANCELLED
        self.error_code = AdapterErrorCode.CANCELLED.value
        self.finished_at = at

    @property
    def is_failed(self) -> bool:
        """Признак отказа для расчёта итогового статуса коллекции."""
        return self.status is OperationStatus.FAILED


@dataclass(slots=True)
class Collection:
    """Коллекция — один запуск сбора (таблица `collections`)."""

    collection_id: str
    idempotency_key: str
    query_text: str
    mode: CollectionMode
    terms: SearchTerms
    limits: CollectionLimits
    status: OperationStatus = OperationStatus.PENDING
    cancel_requested: bool = False
    lease: Lease | None = None
    documents_total: int = 0
    http_requests_total: int = 0
    error_code: str = ""
    error_message: str = ""
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    sources: tuple[SourceKey, ...] = ()

    def can_transition_to(self, status: OperationStatus) -> bool:
        """Допустим ли переход в указанный статус."""
        return status in _ALLOWED_TRANSITIONS[self.status]

    def transition_to(self, status: OperationStatus, at: datetime) -> None:
        """Выполняет переход статуса с проверкой матрицы переходов."""
        if not self.can_transition_to(status):
            raise InvariantViolation(f"недопустимый переход {self.status} → {status}")
        self.status = status
        if status is OperationStatus.RUNNING and self.started_at is None:
            self.started_at = at
        if status.is_terminal:
            self.finished_at = at
            self.lease = None

    def finish(
        self, status: OperationStatus, at: datetime, error_code: str = "", error_message: str = ""
    ) -> None:
        """Терминальное завершение коллекции с кодом ошибки (пустой при успехе)."""
        if not status.is_terminal:
            raise InvariantViolation("finish требует терминальный статус")
        self.transition_to(status, at)
        self.error_code = error_code
        self.error_message = error_message[:500]

    @property
    def is_terminal(self) -> bool:
        """Достигнут ли терминальный статус."""
        return self.status.is_terminal


def compute_final_status(
    runs: list[AdapterRun],
    documents_total: int,
    *,
    cancelled: bool,
    budget_exhausted: bool,
) -> tuple[OperationStatus, str, str]:
    """Итоговый статус коллекции, код и сообщение ошибки (§7 HANDOFF, §9.2 ТЗ).

    CANCELLED — по требованию пользователя; FAILED — документов нет либо все адаптеры отказали;
    PARTIAL — часть адаптеров отказала или исчерпан бюджет времени при наличии документов;
    COMPLETED — все адаптеры завершились и бюджет не исчерпан.
    """
    if cancelled:
        return OperationStatus.CANCELLED, CollectionErrorCode.CANCELLED.value, "сбор отменён по запросу"
    failed = [run for run in runs if run.is_failed]
    if documents_total <= 0:
        code = failed[0].error_code if failed else CollectionErrorCode.NO_DOCUMENTS.value
        message = failed[0].error_message if failed else "ни один источник не вернул документов"
        return OperationStatus.FAILED, code, message
    if failed:
        sources = ",".join(run.source_key.value for run in failed)
        return OperationStatus.PARTIAL, failed[0].error_code, f"отказавшие адаптеры: {sources}"
    if budget_exhausted:
        return (
            OperationStatus.PARTIAL,
            AdapterErrorCode.BUDGET_EXHAUSTED.value,
            "исчерпан бюджет времени сбора",
        )
    return OperationStatus.COMPLETED, "", ""
