from google.protobuf import timestamp_pb2 as _timestamp_pb2
from weaksignals.common.v1 import common_pb2 as _common_pb2
from weaksignals.analyzer.v1 import analyzer_pb2 as _analyzer_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class InsightStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    INSIGHT_STATUS_UNSPECIFIED: _ClassVar[InsightStatus]
    INSIGHT_STATUS_GENERATED: _ClassVar[InsightStatus]
    INSIGHT_STATUS_FALLBACK_EXTRACTIVE: _ClassVar[InsightStatus]

class SummaryKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SUMMARY_KIND_UNSPECIFIED: _ClassVar[SummaryKind]
    SUMMARY_KIND_ORIGINAL_RU: _ClassVar[SummaryKind]
    SUMMARY_KIND_GENERATIVE_SUMMARY: _ClassVar[SummaryKind]
    SUMMARY_KIND_EXTRACTIVE: _ClassVar[SummaryKind]
INSIGHT_STATUS_UNSPECIFIED: InsightStatus
INSIGHT_STATUS_GENERATED: InsightStatus
INSIGHT_STATUS_FALLBACK_EXTRACTIVE: InsightStatus
SUMMARY_KIND_UNSPECIFIED: SummaryKind
SUMMARY_KIND_ORIGINAL_RU: SummaryKind
SUMMARY_KIND_GENERATIVE_SUMMARY: SummaryKind
SUMMARY_KIND_EXTRACTIVE: SummaryKind

class ExpandQueryRequest(_message.Message):
    __slots__ = ("query_text",)
    QUERY_TEXT_FIELD_NUMBER: _ClassVar[int]
    query_text: str
    def __init__(self, query_text: _Optional[str] = ...) -> None: ...

class ExpandQueryResponse(_message.Message):
    __slots__ = ("ru_terms", "en_terms", "domain_tags", "provenance", "used_fallback")
    RU_TERMS_FIELD_NUMBER: _ClassVar[int]
    EN_TERMS_FIELD_NUMBER: _ClassVar[int]
    DOMAIN_TAGS_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    USED_FALLBACK_FIELD_NUMBER: _ClassVar[int]
    ru_terms: _containers.RepeatedScalarFieldContainer[str]
    en_terms: _containers.RepeatedScalarFieldContainer[str]
    domain_tags: _containers.RepeatedScalarFieldContainer[str]
    provenance: Provenance
    used_fallback: bool
    def __init__(self, ru_terms: _Optional[_Iterable[str]] = ..., en_terms: _Optional[_Iterable[str]] = ..., domain_tags: _Optional[_Iterable[str]] = ..., provenance: _Optional[_Union[Provenance, _Mapping]] = ..., used_fallback: bool = ...) -> None: ...

class EvidenceDocument(_message.Message):
    __slots__ = ("document_id", "title", "url", "text", "language_code", "published_at", "source_type", "trust_level")
    DOCUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_CODE_FIELD_NUMBER: _ClassVar[int]
    PUBLISHED_AT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    TRUST_LEVEL_FIELD_NUMBER: _ClassVar[int]
    document_id: str
    title: str
    url: str
    text: str
    language_code: str
    published_at: _timestamp_pb2.Timestamp
    source_type: _common_pb2.SourceType
    trust_level: _common_pb2.TrustLevel
    def __init__(self, document_id: _Optional[str] = ..., title: _Optional[str] = ..., url: _Optional[str] = ..., text: _Optional[str] = ..., language_code: _Optional[str] = ..., published_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., source_type: _Optional[_Union[_common_pb2.SourceType, str]] = ..., trust_level: _Optional[_Union[_common_pb2.TrustLevel, str]] = ...) -> None: ...

class CandidateContext(_message.Message):
    __slots__ = ("candidate_id", "title", "keyphrases", "score", "decision", "top_features", "query_text")
    CANDIDATE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    KEYPHRASES_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    DECISION_FIELD_NUMBER: _ClassVar[int]
    TOP_FEATURES_FIELD_NUMBER: _ClassVar[int]
    QUERY_TEXT_FIELD_NUMBER: _ClassVar[int]
    candidate_id: str
    title: str
    keyphrases: _containers.RepeatedScalarFieldContainer[str]
    score: float
    decision: _analyzer_pb2.Decision
    top_features: _containers.RepeatedCompositeFieldContainer[_analyzer_pb2.FeatureContribution]
    query_text: str
    def __init__(self, candidate_id: _Optional[str] = ..., title: _Optional[str] = ..., keyphrases: _Optional[_Iterable[str]] = ..., score: _Optional[float] = ..., decision: _Optional[_Union[_analyzer_pb2.Decision, str]] = ..., top_features: _Optional[_Iterable[_Union[_analyzer_pb2.FeatureContribution, _Mapping]]] = ..., query_text: _Optional[str] = ...) -> None: ...

class GenerateInsightRequest(_message.Message):
    __slots__ = ("idempotency_key", "candidate", "evidence", "allow_fallback", "max_output_tokens")
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    CANDIDATE_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    ALLOW_FALLBACK_FIELD_NUMBER: _ClassVar[int]
    MAX_OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    idempotency_key: str
    candidate: CandidateContext
    evidence: _containers.RepeatedCompositeFieldContainer[EvidenceDocument]
    allow_fallback: bool
    max_output_tokens: int
    def __init__(self, idempotency_key: _Optional[str] = ..., candidate: _Optional[_Union[CandidateContext, _Mapping]] = ..., evidence: _Optional[_Iterable[_Union[EvidenceDocument, _Mapping]]] = ..., allow_fallback: bool = ..., max_output_tokens: _Optional[int] = ...) -> None: ...

class Narrative(_message.Message):
    __slots__ = ("title_ru", "description_ru", "advantage_ru", "case_example_ru", "case_document_id", "explanation_ru")
    TITLE_RU_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_RU_FIELD_NUMBER: _ClassVar[int]
    ADVANTAGE_RU_FIELD_NUMBER: _ClassVar[int]
    CASE_EXAMPLE_RU_FIELD_NUMBER: _ClassVar[int]
    CASE_DOCUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    EXPLANATION_RU_FIELD_NUMBER: _ClassVar[int]
    title_ru: str
    description_ru: str
    advantage_ru: str
    case_example_ru: str
    case_document_id: str
    explanation_ru: str
    def __init__(self, title_ru: _Optional[str] = ..., description_ru: _Optional[str] = ..., advantage_ru: _Optional[str] = ..., case_example_ru: _Optional[str] = ..., case_document_id: _Optional[str] = ..., explanation_ru: _Optional[str] = ...) -> None: ...

class SourceSummary(_message.Message):
    __slots__ = ("document_id", "summary_ru", "kind")
    DOCUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_RU_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    document_id: str
    summary_ru: str
    kind: SummaryKind
    def __init__(self, document_id: _Optional[str] = ..., summary_ru: _Optional[str] = ..., kind: _Optional[_Union[SummaryKind, str]] = ...) -> None: ...

class GroundingCheck(_message.Message):
    __slots__ = ("passed", "unsupported_numbers", "unknown_document_refs", "features_mentioned")
    PASSED_FIELD_NUMBER: _ClassVar[int]
    UNSUPPORTED_NUMBERS_FIELD_NUMBER: _ClassVar[int]
    UNKNOWN_DOCUMENT_REFS_FIELD_NUMBER: _ClassVar[int]
    FEATURES_MENTIONED_FIELD_NUMBER: _ClassVar[int]
    passed: bool
    unsupported_numbers: int
    unknown_document_refs: int
    features_mentioned: int
    def __init__(self, passed: bool = ..., unsupported_numbers: _Optional[int] = ..., unknown_document_refs: _Optional[int] = ..., features_mentioned: _Optional[int] = ...) -> None: ...

class Provenance(_message.Message):
    __slots__ = ("provider", "model", "prompt_version", "prompt_tokens", "completion_tokens", "attempts", "latency_ms")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    PROMPT_VERSION_FIELD_NUMBER: _ClassVar[int]
    PROMPT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_TOKENS_FIELD_NUMBER: _ClassVar[int]
    ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    LATENCY_MS_FIELD_NUMBER: _ClassVar[int]
    provider: str
    model: str
    prompt_version: str
    prompt_tokens: int
    completion_tokens: int
    attempts: int
    latency_ms: int
    def __init__(self, provider: _Optional[str] = ..., model: _Optional[str] = ..., prompt_version: _Optional[str] = ..., prompt_tokens: _Optional[int] = ..., completion_tokens: _Optional[int] = ..., attempts: _Optional[int] = ..., latency_ms: _Optional[int] = ...) -> None: ...

class GenerateInsightResponse(_message.Message):
    __slots__ = ("insight_id", "status", "narrative", "source_summaries", "grounding", "provenance", "from_cache")
    INSIGHT_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    NARRATIVE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_SUMMARIES_FIELD_NUMBER: _ClassVar[int]
    GROUNDING_FIELD_NUMBER: _ClassVar[int]
    PROVENANCE_FIELD_NUMBER: _ClassVar[int]
    FROM_CACHE_FIELD_NUMBER: _ClassVar[int]
    insight_id: str
    status: InsightStatus
    narrative: Narrative
    source_summaries: _containers.RepeatedCompositeFieldContainer[SourceSummary]
    grounding: GroundingCheck
    provenance: Provenance
    from_cache: bool
    def __init__(self, insight_id: _Optional[str] = ..., status: _Optional[_Union[InsightStatus, str]] = ..., narrative: _Optional[_Union[Narrative, _Mapping]] = ..., source_summaries: _Optional[_Iterable[_Union[SourceSummary, _Mapping]]] = ..., grounding: _Optional[_Union[GroundingCheck, _Mapping]] = ..., provenance: _Optional[_Union[Provenance, _Mapping]] = ..., from_cache: bool = ...) -> None: ...

class GetProviderStatusRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ProviderStatus(_message.Message):
    __slots__ = ("provider", "enabled", "healthy", "max_concurrency", "in_flight", "queued", "last_error", "last_success_at")
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    ENABLED_FIELD_NUMBER: _ClassVar[int]
    HEALTHY_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    IN_FLIGHT_FIELD_NUMBER: _ClassVar[int]
    QUEUED_FIELD_NUMBER: _ClassVar[int]
    LAST_ERROR_FIELD_NUMBER: _ClassVar[int]
    LAST_SUCCESS_AT_FIELD_NUMBER: _ClassVar[int]
    provider: str
    enabled: bool
    healthy: bool
    max_concurrency: int
    in_flight: int
    queued: int
    last_error: str
    last_success_at: _timestamp_pb2.Timestamp
    def __init__(self, provider: _Optional[str] = ..., enabled: bool = ..., healthy: bool = ..., max_concurrency: _Optional[int] = ..., in_flight: _Optional[int] = ..., queued: _Optional[int] = ..., last_error: _Optional[str] = ..., last_success_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class GetProviderStatusResponse(_message.Message):
    __slots__ = ("providers", "active_provider")
    PROVIDERS_FIELD_NUMBER: _ClassVar[int]
    ACTIVE_PROVIDER_FIELD_NUMBER: _ClassVar[int]
    providers: _containers.RepeatedCompositeFieldContainer[ProviderStatus]
    active_provider: str
    def __init__(self, providers: _Optional[_Iterable[_Union[ProviderStatus, _Mapping]]] = ..., active_provider: _Optional[str] = ...) -> None: ...
