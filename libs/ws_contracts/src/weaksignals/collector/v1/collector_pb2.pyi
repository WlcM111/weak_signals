from google.protobuf import timestamp_pb2 as _timestamp_pb2
from weaksignals.common.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CollectionMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    COLLECTION_MODE_UNSPECIFIED: _ClassVar[CollectionMode]
    COLLECTION_MODE_SEARCH: _ClassVar[CollectionMode]
    COLLECTION_MODE_ENRICHMENT: _ClassVar[CollectionMode]
COLLECTION_MODE_UNSPECIFIED: CollectionMode
COLLECTION_MODE_SEARCH: CollectionMode
COLLECTION_MODE_ENRICHMENT: CollectionMode

class SearchTerms(_message.Message):
    __slots__ = ("ru", "en")
    RU_FIELD_NUMBER: _ClassVar[int]
    EN_FIELD_NUMBER: _ClassVar[int]
    ru: _containers.RepeatedScalarFieldContainer[str]
    en: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, ru: _Optional[_Iterable[str]] = ..., en: _Optional[_Iterable[str]] = ...) -> None: ...

class CollectionLimits(_message.Message):
    __slots__ = ("max_documents_per_source", "max_total_documents", "time_budget_seconds", "published_since_year")
    MAX_DOCUMENTS_PER_SOURCE_FIELD_NUMBER: _ClassVar[int]
    MAX_TOTAL_DOCUMENTS_FIELD_NUMBER: _ClassVar[int]
    TIME_BUDGET_SECONDS_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_SINCE_YEAR_FIELD_NUMBER: _ClassVar[int]
    max_documents_per_source: int
    max_total_documents: int
    time_budget_seconds: int
    published_since_year: int
    def __init__(self, max_documents_per_source: _Optional[int] = ..., max_total_documents: _Optional[int] = ..., time_budget_seconds: _Optional[int] = ..., published_since_year: _Optional[int] = ...) -> None: ...

class StartCollectionRequest(_message.Message):
    __slots__ = ("idempotency_key", "query_text", "terms", "mode", "limits", "sources")
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    QUERY_TEXT_FIELD_NUMBER: _ClassVar[int]
    TERMS_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    LIMITS_FIELD_NUMBER: _ClassVar[int]
    SOURCES_FIELD_NUMBER: _ClassVar[int]
    idempotency_key: str
    query_text: str
    terms: SearchTerms
    mode: CollectionMode
    limits: CollectionLimits
    sources: _containers.RepeatedScalarFieldContainer[_common_pb2.SourceKey]
    def __init__(self, idempotency_key: _Optional[str] = ..., query_text: _Optional[str] = ..., terms: _Optional[_Union[SearchTerms, _Mapping]] = ..., mode: _Optional[_Union[CollectionMode, str]] = ..., limits: _Optional[_Union[CollectionLimits, _Mapping]] = ..., sources: _Optional[_Iterable[_Union[_common_pb2.SourceKey, str]]] = ...) -> None: ...

class StartCollectionResponse(_message.Message):
    __slots__ = ("collection_id", "status", "already_existed")
    COLLECTION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ALREADY_EXISTED_FIELD_NUMBER: _ClassVar[int]
    collection_id: str
    status: _common_pb2.OperationStatus
    already_existed: bool
    def __init__(self, collection_id: _Optional[str] = ..., status: _Optional[_Union[_common_pb2.OperationStatus, str]] = ..., already_existed: bool = ...) -> None: ...

class GetCollectionRequest(_message.Message):
    __slots__ = ("collection_id",)
    COLLECTION_ID_FIELD_NUMBER: _ClassVar[int]
    collection_id: str
    def __init__(self, collection_id: _Optional[str] = ...) -> None: ...

class AdapterRunSummary(_message.Message):
    __slots__ = ("source_key", "status", "http_requests", "documents_found", "documents_new", "error_code", "error_message")
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    HTTP_REQUESTS_FIELD_NUMBER: _ClassVar[int]
    DOCUMENTS_FOUND_FIELD_NUMBER: _ClassVar[int]
    DOCUMENTS_NEW_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    source_key: _common_pb2.SourceKey
    status: _common_pb2.OperationStatus
    http_requests: int
    documents_found: int
    documents_new: int
    error_code: str
    error_message: str
    def __init__(self, source_key: _Optional[_Union[_common_pb2.SourceKey, str]] = ..., status: _Optional[_Union[_common_pb2.OperationStatus, str]] = ..., http_requests: _Optional[int] = ..., documents_found: _Optional[int] = ..., documents_new: _Optional[int] = ..., error_code: _Optional[str] = ..., error_message: _Optional[str] = ...) -> None: ...

class GetCollectionResponse(_message.Message):
    __slots__ = ("collection_id", "status", "documents_total", "sources_processed", "http_requests_total", "adapter_runs", "started_at", "finished_at", "error_code", "error_message")
    COLLECTION_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    DOCUMENTS_TOTAL_FIELD_NUMBER: _ClassVar[int]
    SOURCES_PROCESSED_FIELD_NUMBER: _ClassVar[int]
    HTTP_REQUESTS_TOTAL_FIELD_NUMBER: _ClassVar[int]
    ADAPTER_RUNS_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    FINISHED_AT_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    collection_id: str
    status: _common_pb2.OperationStatus
    documents_total: int
    sources_processed: int
    http_requests_total: int
    adapter_runs: _containers.RepeatedCompositeFieldContainer[AdapterRunSummary]
    started_at: _timestamp_pb2.Timestamp
    finished_at: _timestamp_pb2.Timestamp
    error_code: str
    error_message: str
    def __init__(self, collection_id: _Optional[str] = ..., status: _Optional[_Union[_common_pb2.OperationStatus, str]] = ..., documents_total: _Optional[int] = ..., sources_processed: _Optional[int] = ..., http_requests_total: _Optional[int] = ..., adapter_runs: _Optional[_Iterable[_Union[AdapterRunSummary, _Mapping]]] = ..., started_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., finished_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., error_code: _Optional[str] = ..., error_message: _Optional[str] = ...) -> None: ...

class StreamDocumentsRequest(_message.Message):
    __slots__ = ("collection_id", "chunk_size", "page_token")
    COLLECTION_ID_FIELD_NUMBER: _ClassVar[int]
    CHUNK_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    collection_id: str
    chunk_size: int
    page_token: str
    def __init__(self, collection_id: _Optional[str] = ..., chunk_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class DocumentChunk(_message.Message):
    __slots__ = ("documents", "next_page_token", "last")
    DOCUMENTS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    LAST_FIELD_NUMBER: _ClassVar[int]
    documents: _containers.RepeatedCompositeFieldContainer[_common_pb2.Document]
    next_page_token: str
    last: bool
    def __init__(self, documents: _Optional[_Iterable[_Union[_common_pb2.Document, _Mapping]]] = ..., next_page_token: _Optional[str] = ..., last: bool = ...) -> None: ...

class GetDocumentsRequest(_message.Message):
    __slots__ = ("document_ids",)
    DOCUMENT_IDS_FIELD_NUMBER: _ClassVar[int]
    document_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, document_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class GetDocumentsResponse(_message.Message):
    __slots__ = ("documents", "missing_document_ids")
    DOCUMENTS_FIELD_NUMBER: _ClassVar[int]
    MISSING_DOCUMENT_IDS_FIELD_NUMBER: _ClassVar[int]
    documents: _containers.RepeatedCompositeFieldContainer[_common_pb2.Document]
    missing_document_ids: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, documents: _Optional[_Iterable[_Union[_common_pb2.Document, _Mapping]]] = ..., missing_document_ids: _Optional[_Iterable[str]] = ...) -> None: ...

class CancelCollectionRequest(_message.Message):
    __slots__ = ("collection_id", "reason")
    COLLECTION_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    collection_id: str
    reason: str
    def __init__(self, collection_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class CancelCollectionResponse(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: _common_pb2.OperationStatus
    def __init__(self, status: _Optional[_Union[_common_pb2.OperationStatus, str]] = ...) -> None: ...

class CheckEncyclopediaRequest(_message.Message):
    __slots__ = ("titles", "language_code")
    TITLES_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_CODE_FIELD_NUMBER: _ClassVar[int]
    titles: _containers.RepeatedScalarFieldContainer[str]
    language_code: str
    def __init__(self, titles: _Optional[_Iterable[str]] = ..., language_code: _Optional[str] = ...) -> None: ...

class EncyclopediaHit(_message.Message):
    __slots__ = ("title", "exists", "page_url", "pageviews_30d", "created_at")
    TITLE_FIELD_NUMBER: _ClassVar[int]
    EXISTS_FIELD_NUMBER: _ClassVar[int]
    PAGE_URL_FIELD_NUMBER: _ClassVar[int]
    PAGEVIEWS_30D_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    title: str
    exists: bool
    page_url: str
    pageviews_30d: int
    created_at: _timestamp_pb2.Timestamp
    def __init__(self, title: _Optional[str] = ..., exists: bool = ..., page_url: _Optional[str] = ..., pageviews_30d: _Optional[int] = ..., created_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class CheckEncyclopediaResponse(_message.Message):
    __slots__ = ("hits",)
    HITS_FIELD_NUMBER: _ClassVar[int]
    hits: _containers.RepeatedCompositeFieldContainer[EncyclopediaHit]
    def __init__(self, hits: _Optional[_Iterable[_Union[EncyclopediaHit, _Mapping]]] = ...) -> None: ...
