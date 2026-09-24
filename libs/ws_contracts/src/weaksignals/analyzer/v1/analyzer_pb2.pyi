from google.protobuf import timestamp_pb2 as _timestamp_pb2
from weaksignals.common.v1 import common_pb2 as _common_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Decision(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DECISION_UNSPECIFIED: _ClassVar[Decision]
    DECISION_WEAK_SIGNAL: _ClassVar[Decision]
    DECISION_MATURE: _ClassVar[Decision]
    DECISION_HYPE_OR_NOISE: _ClassVar[Decision]
    DECISION_INSUFFICIENT_EVIDENCE: _ClassVar[Decision]
    DECISION_OFF_TOPIC: _ClassVar[Decision]

class DecisionReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DECISION_REASON_UNSPECIFIED: _ClassVar[DecisionReason]
    DECISION_REASON_MODEL_SCORE: _ClassVar[DecisionReason]
    DECISION_REASON_ENCYCLOPEDIA_MATURE: _ClassVar[DecisionReason]
    DECISION_REASON_MARKET_LEADERS: _ClassVar[DecisionReason]
    DECISION_REASON_MATURITY_LEXICON: _ClassVar[DecisionReason]
    DECISION_REASON_MARKETING_DOMINANT: _ClassVar[DecisionReason]
    DECISION_REASON_HYPE_LEXICON: _ClassVar[DecisionReason]
    DECISION_REASON_NO_TRUSTED_SOURCE: _ClassVar[DecisionReason]
    DECISION_REASON_SINGLE_SOURCE: _ClassVar[DecisionReason]
    DECISION_REASON_LOW_QUERY_RELEVANCE: _ClassVar[DecisionReason]
DECISION_UNSPECIFIED: Decision
DECISION_WEAK_SIGNAL: Decision
DECISION_MATURE: Decision
DECISION_HYPE_OR_NOISE: Decision
DECISION_INSUFFICIENT_EVIDENCE: Decision
DECISION_OFF_TOPIC: Decision
DECISION_REASON_UNSPECIFIED: DecisionReason
DECISION_REASON_MODEL_SCORE: DecisionReason
DECISION_REASON_ENCYCLOPEDIA_MATURE: DecisionReason
DECISION_REASON_MARKET_LEADERS: DecisionReason
DECISION_REASON_MATURITY_LEXICON: DecisionReason
DECISION_REASON_MARKETING_DOMINANT: DecisionReason
DECISION_REASON_HYPE_LEXICON: DecisionReason
DECISION_REASON_NO_TRUSTED_SOURCE: DecisionReason
DECISION_REASON_SINGLE_SOURCE: DecisionReason
DECISION_REASON_LOW_QUERY_RELEVANCE: DecisionReason

class AnalysisParams(_message.Message):
    __slots__ = ("top_n", "max_candidates", "weak_signal_threshold", "min_evidence_documents")
    TOP_N_FIELD_NUMBER: _ClassVar[int]
    MAX_CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    WEAK_SIGNAL_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    MIN_EVIDENCE_DOCUMENTS_FIELD_NUMBER: _ClassVar[int]
    top_n: int
    max_candidates: int
    weak_signal_threshold: float
    min_evidence_documents: int
    def __init__(self, top_n: _Optional[int] = ..., max_candidates: _Optional[int] = ..., weak_signal_threshold: _Optional[float] = ..., min_evidence_documents: _Optional[int] = ...) -> None: ...

class StartAnalysisRequest(_message.Message):
    __slots__ = ("idempotency_key", "collection_id", "query_text", "params")
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    COLLECTION_ID_FIELD_NUMBER: _ClassVar[int]
    QUERY_TEXT_FIELD_NUMBER: _ClassVar[int]
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    idempotency_key: str
    collection_id: str
    query_text: str
    params: AnalysisParams
    def __init__(self, idempotency_key: _Optional[str] = ..., collection_id: _Optional[str] = ..., query_text: _Optional[str] = ..., params: _Optional[_Union[AnalysisParams, _Mapping]] = ...) -> None: ...

class StartAnalysisResponse(_message.Message):
    __slots__ = ("analysis_id", "status", "already_existed", "model_version_id")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ALREADY_EXISTED_FIELD_NUMBER: _ClassVar[int]
    MODEL_VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    status: _common_pb2.OperationStatus
    already_existed: bool
    model_version_id: str
    def __init__(self, analysis_id: _Optional[str] = ..., status: _Optional[_Union[_common_pb2.OperationStatus, str]] = ..., already_existed: bool = ..., model_version_id: _Optional[str] = ...) -> None: ...

class GetAnalysisRequest(_message.Message):
    __slots__ = ("analysis_id",)
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    def __init__(self, analysis_id: _Optional[str] = ...) -> None: ...

class AnalysisStats(_message.Message):
    __slots__ = ("documents_input", "documents_after_dedup", "clusters_total", "candidates_scored", "weak_signals_total", "weak_signals_confident", "excluded_mature", "excluded_hype_or_noise", "excluded_insufficient_evidence", "excluded_off_topic", "duration_ms")
    DOCUMENTS_INPUT_FIELD_NUMBER: _ClassVar[int]
    DOCUMENTS_AFTER_DEDUP_FIELD_NUMBER: _ClassVar[int]
    CLUSTERS_TOTAL_FIELD_NUMBER: _ClassVar[int]
    CANDIDATES_SCORED_FIELD_NUMBER: _ClassVar[int]
    WEAK_SIGNALS_TOTAL_FIELD_NUMBER: _ClassVar[int]
    WEAK_SIGNALS_CONFIDENT_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_MATURE_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_HYPE_OR_NOISE_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_INSUFFICIENT_EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    EXCLUDED_OFF_TOPIC_FIELD_NUMBER: _ClassVar[int]
    DURATION_MS_FIELD_NUMBER: _ClassVar[int]
    documents_input: int
    documents_after_dedup: int
    clusters_total: int
    candidates_scored: int
    weak_signals_total: int
    weak_signals_confident: int
    excluded_mature: int
    excluded_hype_or_noise: int
    excluded_insufficient_evidence: int
    excluded_off_topic: int
    duration_ms: int
    def __init__(self, documents_input: _Optional[int] = ..., documents_after_dedup: _Optional[int] = ..., clusters_total: _Optional[int] = ..., candidates_scored: _Optional[int] = ..., weak_signals_total: _Optional[int] = ..., weak_signals_confident: _Optional[int] = ..., excluded_mature: _Optional[int] = ..., excluded_hype_or_noise: _Optional[int] = ..., excluded_insufficient_evidence: _Optional[int] = ..., excluded_off_topic: _Optional[int] = ..., duration_ms: _Optional[int] = ...) -> None: ...

class GetAnalysisResponse(_message.Message):
    __slots__ = ("analysis_id", "status", "model_version_id", "stats", "error_code", "error_message", "started_at", "finished_at")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    MODEL_VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    STATS_FIELD_NUMBER: _ClassVar[int]
    ERROR_CODE_FIELD_NUMBER: _ClassVar[int]
    ERROR_MESSAGE_FIELD_NUMBER: _ClassVar[int]
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    FINISHED_AT_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    status: _common_pb2.OperationStatus
    model_version_id: str
    stats: AnalysisStats
    error_code: str
    error_message: str
    started_at: _timestamp_pb2.Timestamp
    finished_at: _timestamp_pb2.Timestamp
    def __init__(self, analysis_id: _Optional[str] = ..., status: _Optional[_Union[_common_pb2.OperationStatus, str]] = ..., model_version_id: _Optional[str] = ..., stats: _Optional[_Union[AnalysisStats, _Mapping]] = ..., error_code: _Optional[str] = ..., error_message: _Optional[str] = ..., started_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., finished_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class FeatureContribution(_message.Message):
    __slots__ = ("feature_name", "value", "contribution", "label_ru", "direction")
    FEATURE_NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    CONTRIBUTION_FIELD_NUMBER: _ClassVar[int]
    LABEL_RU_FIELD_NUMBER: _ClassVar[int]
    DIRECTION_FIELD_NUMBER: _ClassVar[int]
    feature_name: str
    value: float
    contribution: float
    label_ru: str
    direction: str
    def __init__(self, feature_name: _Optional[str] = ..., value: _Optional[float] = ..., contribution: _Optional[float] = ..., label_ru: _Optional[str] = ..., direction: _Optional[str] = ...) -> None: ...

class Evidence(_message.Message):
    __slots__ = ("document_id", "snippet", "similarity", "source_type", "trust_level")
    DOCUMENT_ID_FIELD_NUMBER: _ClassVar[int]
    SNIPPET_FIELD_NUMBER: _ClassVar[int]
    SIMILARITY_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    TRUST_LEVEL_FIELD_NUMBER: _ClassVar[int]
    document_id: str
    snippet: str
    similarity: float
    source_type: _common_pb2.SourceType
    trust_level: _common_pb2.TrustLevel
    def __init__(self, document_id: _Optional[str] = ..., snippet: _Optional[str] = ..., similarity: _Optional[float] = ..., source_type: _Optional[_Union[_common_pb2.SourceType, str]] = ..., trust_level: _Optional[_Union[_common_pb2.TrustLevel, str]] = ...) -> None: ...

class SourceTypeCount(_message.Message):
    __slots__ = ("source_type", "count")
    SOURCE_TYPE_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    source_type: _common_pb2.SourceType
    count: int
    def __init__(self, source_type: _Optional[_Union[_common_pb2.SourceType, str]] = ..., count: _Optional[int] = ...) -> None: ...

class YearCount(_message.Message):
    __slots__ = ("year", "count")
    YEAR_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    year: int
    count: int
    def __init__(self, year: _Optional[int] = ..., count: _Optional[int] = ...) -> None: ...

class Candidate(_message.Message):
    __slots__ = ("candidate_id", "analysis_id", "rank", "title", "keyphrases", "score", "decision", "decision_reason", "decision_explanation_ru", "features", "evidence", "document_count", "source_type_counts", "year_counts", "query_relevance", "predicted_stage", "predicted_trend")
    CANDIDATE_ID_FIELD_NUMBER: _ClassVar[int]
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    RANK_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    KEYPHRASES_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    DECISION_FIELD_NUMBER: _ClassVar[int]
    DECISION_REASON_FIELD_NUMBER: _ClassVar[int]
    DECISION_EXPLANATION_RU_FIELD_NUMBER: _ClassVar[int]
    FEATURES_FIELD_NUMBER: _ClassVar[int]
    EVIDENCE_FIELD_NUMBER: _ClassVar[int]
    DOCUMENT_COUNT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_TYPE_COUNTS_FIELD_NUMBER: _ClassVar[int]
    YEAR_COUNTS_FIELD_NUMBER: _ClassVar[int]
    QUERY_RELEVANCE_FIELD_NUMBER: _ClassVar[int]
    PREDICTED_STAGE_FIELD_NUMBER: _ClassVar[int]
    PREDICTED_TREND_FIELD_NUMBER: _ClassVar[int]
    candidate_id: str
    analysis_id: str
    rank: int
    title: str
    keyphrases: _containers.RepeatedScalarFieldContainer[str]
    score: float
    decision: Decision
    decision_reason: DecisionReason
    decision_explanation_ru: str
    features: _containers.RepeatedCompositeFieldContainer[FeatureContribution]
    evidence: _containers.RepeatedCompositeFieldContainer[Evidence]
    document_count: int
    source_type_counts: _containers.RepeatedCompositeFieldContainer[SourceTypeCount]
    year_counts: _containers.RepeatedCompositeFieldContainer[YearCount]
    query_relevance: float
    predicted_stage: int
    predicted_trend: int
    def __init__(self, candidate_id: _Optional[str] = ..., analysis_id: _Optional[str] = ..., rank: _Optional[int] = ..., title: _Optional[str] = ..., keyphrases: _Optional[_Iterable[str]] = ..., score: _Optional[float] = ..., decision: _Optional[_Union[Decision, str]] = ..., decision_reason: _Optional[_Union[DecisionReason, str]] = ..., decision_explanation_ru: _Optional[str] = ..., features: _Optional[_Iterable[_Union[FeatureContribution, _Mapping]]] = ..., evidence: _Optional[_Iterable[_Union[Evidence, _Mapping]]] = ..., document_count: _Optional[int] = ..., source_type_counts: _Optional[_Iterable[_Union[SourceTypeCount, _Mapping]]] = ..., year_counts: _Optional[_Iterable[_Union[YearCount, _Mapping]]] = ..., query_relevance: _Optional[float] = ..., predicted_stage: _Optional[int] = ..., predicted_trend: _Optional[int] = ...) -> None: ...

class ListCandidatesRequest(_message.Message):
    __slots__ = ("analysis_id", "include_excluded", "page")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    INCLUDE_EXCLUDED_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    include_excluded: bool
    page: _common_pb2.PageRequest
    def __init__(self, analysis_id: _Optional[str] = ..., include_excluded: bool = ..., page: _Optional[_Union[_common_pb2.PageRequest, _Mapping]] = ...) -> None: ...

class ListCandidatesResponse(_message.Message):
    __slots__ = ("candidates", "page")
    CANDIDATES_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    candidates: _containers.RepeatedCompositeFieldContainer[Candidate]
    page: _common_pb2.PageResponse
    def __init__(self, candidates: _Optional[_Iterable[_Union[Candidate, _Mapping]]] = ..., page: _Optional[_Union[_common_pb2.PageResponse, _Mapping]] = ...) -> None: ...

class CancelAnalysisRequest(_message.Message):
    __slots__ = ("analysis_id", "reason")
    ANALYSIS_ID_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    analysis_id: str
    reason: str
    def __init__(self, analysis_id: _Optional[str] = ..., reason: _Optional[str] = ...) -> None: ...

class CancelAnalysisResponse(_message.Message):
    __slots__ = ("status",)
    STATUS_FIELD_NUMBER: _ClassVar[int]
    status: _common_pb2.OperationStatus
    def __init__(self, status: _Optional[_Union[_common_pb2.OperationStatus, str]] = ...) -> None: ...

class ScoreTextRequest(_message.Message):
    __slots__ = ("title", "description", "with_enrichment")
    TITLE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    WITH_ENRICHMENT_FIELD_NUMBER: _ClassVar[int]
    title: str
    description: str
    with_enrichment: bool
    def __init__(self, title: _Optional[str] = ..., description: _Optional[str] = ..., with_enrichment: bool = ...) -> None: ...

class ScoreTextResponse(_message.Message):
    __slots__ = ("score", "decision", "decision_reason", "features", "model_version_id", "predicted_stage", "predicted_trend", "enrichment_applied")
    SCORE_FIELD_NUMBER: _ClassVar[int]
    DECISION_FIELD_NUMBER: _ClassVar[int]
    DECISION_REASON_FIELD_NUMBER: _ClassVar[int]
    FEATURES_FIELD_NUMBER: _ClassVar[int]
    MODEL_VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    PREDICTED_STAGE_FIELD_NUMBER: _ClassVar[int]
    PREDICTED_TREND_FIELD_NUMBER: _ClassVar[int]
    ENRICHMENT_APPLIED_FIELD_NUMBER: _ClassVar[int]
    score: float
    decision: Decision
    decision_reason: DecisionReason
    features: _containers.RepeatedCompositeFieldContainer[FeatureContribution]
    model_version_id: str
    predicted_stage: int
    predicted_trend: int
    enrichment_applied: bool
    def __init__(self, score: _Optional[float] = ..., decision: _Optional[_Union[Decision, str]] = ..., decision_reason: _Optional[_Union[DecisionReason, str]] = ..., features: _Optional[_Iterable[_Union[FeatureContribution, _Mapping]]] = ..., model_version_id: _Optional[str] = ..., predicted_stage: _Optional[int] = ..., predicted_trend: _Optional[int] = ..., enrichment_applied: bool = ...) -> None: ...

class GetModelInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ModelMetrics(_message.Message):
    __slots__ = ("accuracy", "precision", "recall", "f1", "roc_auc", "threshold", "test_size", "evaluation_protocol")
    ACCURACY_FIELD_NUMBER: _ClassVar[int]
    PRECISION_FIELD_NUMBER: _ClassVar[int]
    RECALL_FIELD_NUMBER: _ClassVar[int]
    F1_FIELD_NUMBER: _ClassVar[int]
    ROC_AUC_FIELD_NUMBER: _ClassVar[int]
    THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    TEST_SIZE_FIELD_NUMBER: _ClassVar[int]
    EVALUATION_PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    accuracy: float
    precision: float
    recall: float
    f1: float
    roc_auc: float
    threshold: float
    test_size: int
    evaluation_protocol: str
    def __init__(self, accuracy: _Optional[float] = ..., precision: _Optional[float] = ..., recall: _Optional[float] = ..., f1: _Optional[float] = ..., roc_auc: _Optional[float] = ..., threshold: _Optional[float] = ..., test_size: _Optional[int] = ..., evaluation_protocol: _Optional[str] = ...) -> None: ...

class GetModelInfoResponse(_message.Message):
    __slots__ = ("model_version_id", "model_family", "feature_schema_version", "embedding_model", "dataset_version", "trained_at", "metrics", "feature_names")
    MODEL_VERSION_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_FAMILY_FIELD_NUMBER: _ClassVar[int]
    FEATURE_SCHEMA_VERSION_FIELD_NUMBER: _ClassVar[int]
    EMBEDDING_MODEL_FIELD_NUMBER: _ClassVar[int]
    DATASET_VERSION_FIELD_NUMBER: _ClassVar[int]
    TRAINED_AT_FIELD_NUMBER: _ClassVar[int]
    METRICS_FIELD_NUMBER: _ClassVar[int]
    FEATURE_NAMES_FIELD_NUMBER: _ClassVar[int]
    model_version_id: str
    model_family: str
    feature_schema_version: str
    embedding_model: str
    dataset_version: str
    trained_at: _timestamp_pb2.Timestamp
    metrics: ModelMetrics
    feature_names: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, model_version_id: _Optional[str] = ..., model_family: _Optional[str] = ..., feature_schema_version: _Optional[str] = ..., embedding_model: _Optional[str] = ..., dataset_version: _Optional[str] = ..., trained_at: _Optional[_Union[_timestamp_pb2.Timestamp, _Mapping]] = ..., metrics: _Optional[_Union[ModelMetrics, _Mapping]] = ..., feature_names: _Optional[_Iterable[str]] = ...) -> None: ...
