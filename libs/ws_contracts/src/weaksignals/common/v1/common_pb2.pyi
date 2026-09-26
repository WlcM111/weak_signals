from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SourceType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SOURCE_TYPE_UNSPECIFIED: _ClassVar[SourceType]
    SOURCE_TYPE_SCIENTIFIC_PUBLICATION: _ClassVar[SourceType]
    SOURCE_TYPE_PREPRINT: _ClassVar[SourceType]
    SOURCE_TYPE_PATENT: _ClassVar[SourceType]
    SOURCE_TYPE_NEWS: _ClassVar[SourceType]
    SOURCE_TYPE_INDUSTRY_MEDIA: _ClassVar[SourceType]
    SOURCE_TYPE_CORPORATE_BLOG: _ClassVar[SourceType]
    SOURCE_TYPE_PRESS_RELEASE: _ClassVar[SourceType]
    SOURCE_TYPE_CODE_REPOSITORY: _ClassVar[SourceType]
    SOURCE_TYPE_VACANCY: _ClassVar[SourceType]
    SOURCE_TYPE_ANALYTICAL_REPORT: _ClassVar[SourceType]
    SOURCE_TYPE_GOVERNMENT: _ClassVar[SourceType]
    SOURCE_TYPE_SOCIAL_MEDIA: _ClassVar[SourceType]
    SOURCE_TYPE_ENCYCLOPEDIA: _ClassVar[SourceType]
    SOURCE_TYPE_OTHER: _ClassVar[SourceType]

class TrustLevel(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    TRUST_LEVEL_UNSPECIFIED: _ClassVar[TrustLevel]
    TRUST_LEVEL_HIGH: _ClassVar[TrustLevel]
    TRUST_LEVEL_MEDIUM: _ClassVar[TrustLevel]
    TRUST_LEVEL_LOW: _ClassVar[TrustLevel]

class SourceKey(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SOURCE_KEY_UNSPECIFIED: _ClassVar[SourceKey]
    SOURCE_KEY_OPENALEX: _ClassVar[SourceKey]
    SOURCE_KEY_ARXIV: _ClassVar[SourceKey]
    SOURCE_KEY_PATENTSVIEW: _ClassVar[SourceKey]
    SOURCE_KEY_RSS: _ClassVar[SourceKey]
    SOURCE_KEY_GITHUB: _ClassVar[SourceKey]
    SOURCE_KEY_HH: _ClassVar[SourceKey]
    SOURCE_KEY_WIKIPEDIA: _ClassVar[SourceKey]
    SOURCE_KEY_SEMANTIC_SCHOLAR: _ClassVar[SourceKey]
    SOURCE_KEY_ZENODO: _ClassVar[SourceKey]
    SOURCE_KEY_GDELT: _ClassVar[SourceKey]
    SOURCE_KEY_ROSPATENT: _ClassVar[SourceKey]

class OperationStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    OPERATION_STATUS_UNSPECIFIED: _ClassVar[OperationStatus]
    OPERATION_STATUS_PENDING: _ClassVar[OperationStatus]
    OPERATION_STATUS_RUNNING: _ClassVar[OperationStatus]
    OPERATION_STATUS_COMPLETED: _ClassVar[OperationStatus]
    OPERATION_STATUS_PARTIAL: _ClassVar[OperationStatus]
    OPERATION_STATUS_FAILED: _ClassVar[OperationStatus]
    OPERATION_STATUS_CANCELLED: _ClassVar[OperationStatus]
SOURCE_TYPE_UNSPECIFIED: SourceType
SOURCE_TYPE_SCIENTIFIC_PUBLICATION: SourceType
SOURCE_TYPE_PREPRINT: SourceType
SOURCE_TYPE_PATENT: SourceType
SOURCE_TYPE_NEWS: SourceType
SOURCE_TYPE_INDUSTRY_MEDIA: SourceType
SOURCE_TYPE_CORPORATE_BLOG: SourceType
SOURCE_TYPE_PRESS_RELEASE: SourceType
SOURCE_TYPE_CODE_REPOSITORY: SourceType
SOURCE_TYPE_VACANCY: SourceType
SOURCE_TYPE_ANALYTICAL_REPORT: SourceType
SOURCE_TYPE_GOVERNMENT: SourceType
SOURCE_TYPE_SOCIAL_MEDIA: SourceType
SOURCE_TYPE_ENCYCLOPEDIA: SourceType
SOURCE_TYPE_OTHER: SourceType
TRUST_LEVEL_UNSPECIFIED: TrustLevel
TRUST_LEVEL_HIGH: TrustLevel
TRUST_LEVEL_MEDIUM: TrustLevel
TRUST_LEVEL_LOW: TrustLevel
SOURCE_KEY_UNSPECIFIED: SourceKey
SOURCE_KEY_OPENALEX: SourceKey
SOURCE_KEY_ARXIV: SourceKey
SOURCE_KEY_PATENTSVIEW: SourceKey
SOURCE_KEY_RSS: SourceKey
SOURCE_KEY_GITHUB: SourceKey
SOURCE_KEY_HH: SourceKey
SOURCE_KEY_WIKIPEDIA: SourceKey
SOURCE_KEY_SEMANTIC_SCHOLAR: SourceKey
SOURCE_KEY_ZENODO: SourceKey
SOURCE_KEY_GDELT: SourceKey
SOURCE_KEY_ROSPATENT: SourceKey
OPERATION_STATUS_UNSPECIFIED: OperationStatus
OPERATION_STATUS_PENDING: OperationStatus
OPERATION_STATUS_RUNNING: OperationStatus
OPERATION_STATUS_COMPLETED: OperationStatus
OPERATION_STATUS_PARTIAL: OperationStatus
OPERATION_STATUS_FAILED: OperationStatus
OPERATION_STATUS_CANCELLED: OperationStatus

class Document(_message.Message):
    __slots__ = ("document_id", "url", "title", "text", "language_code", "published_at", "source_key", "source_type", "trust_level", "doi", "citation_count", "engagement_count", "fetched_at", "origin_domain")
    DOCUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_CODE_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_AT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_KEY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    TRUST_LEVEL_FIELD_NUMBER: _ClassVar[int]
    DOI_FIELD_NUMBER: _ClassVar[int]
    CITATION_COUNT_FIELD_NUMBER: _ClassVar[int]
    ENGAGEMENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    FETCHED_AT_FIELD_NUMBER: _ClassVar[int]
    ORIGIN_DOMAIN_FIELD_NUMBER: _ClassVar[int]
    document_id: str
    url: str
    title: str
    text: str
    language_code: str
    published_at: _timestamp_pb2.Timestamp
    source_key: SourceKey
    source_type: SourceType
    trust_level: TrustLevel
    doi: str
    citation_count: int
    engagement_count: int
    fetched_at: _timestamp_pb2.Timestamp
    origin_domain: str
    def __init__(self, document_id: _Optional[str] = ..., url: _Optional[str] = ..., title: _Optional[str] = ..., text: _Optional[str] = ..., language_code: _Optional[str] = ..., published_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., source_key: _Optional[_Union[SourceKey, str]] = ..., source_type: _Optional[_Union[SourceType, str]] = ..., trust_level: _Optional[_Union[TrustLevel, str]] = ..., doi: _Optional[str] = ..., citation_count: _Optional[int] = ..., engagement_count: _Optional[int] = ..., fetched_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., origin_domain: _Optional[str] = ...) -> None: ...

class PageRequest(_message.Message):
    __slots__ = ("page_size", "page_token")
    PAGE_SIZE_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    page_size: int
    page_token: str
    def __init__(self, page_size: _Optional[int] = ..., page_token: _Optional[str] = ...) -> None: ...

class PageResponse(_message.Message):
    __slots__ = ("next_page_token", "total_count")
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    TOTAL_COUNT_FIELD_NUMBER: _ClassVar[int]
    next_page_token: str
    total_count: int
    def __init__(self, next_page_token: _Optional[str] = ..., total_count: _Optional[int] = ...) -> None: ...
