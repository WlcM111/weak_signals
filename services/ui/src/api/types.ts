/** Типы ответов orchestrator по `docs/api/orchestrator.openapi.yaml` (клиентская сторона). */

export type JobStatus =
  | "QUEUED" | "COLLECTING" | "ANALYZING" | "NARRATING"
  | "COMPLETED" | "PARTIAL" | "FAILED" | "CANCELLED";

export type Decision =
  | "WEAK_SIGNAL" | "MATURE" | "HYPE_OR_NOISE" | "INSUFFICIENT_EVIDENCE" | "OFF_TOPIC";

export type Direction = "supports_weak_signal" | "supports_mature" | "neutral";
export type TrustLevel = "HIGH" | "MEDIUM" | "LOW";
export type SummaryKind = "ORIGINAL_RU" | "GENERATIVE_SUMMARY" | "EXTRACTIVE";
export type NarrativeStatus = "GENERATED" | "FALLBACK_EXTRACTIVE";
export type ConfidenceBand = "High" | "Medium" | "Low";

export interface ApiErrorBody {
  code: string;
  message: string;
  request_id: string;
  details?: { field?: string; issue?: string }[];
  retryable?: boolean;
}

export interface JobProgress {
  stage: JobStatus;
  http_requests_total: number;
  sources_processed: number;
  documents_collected: number;
  candidates_found: number;
  narratives_done: number;
  narratives_total: number;
}

export interface Job {
  job_id: string;
  query_id: string;
  query_text: string;
  status: JobStatus;
  attempt: number;
  cancel_requested?: boolean;
  error?: ApiErrorBody | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  progress: JobProgress;
}

export interface JobList { items: Job[]; next_cursor?: string }
export interface JobAccepted { job_id: string; query_id: string; status: JobStatus; created: boolean }

export interface Feature {
  feature_name: string;
  label_ru: string;
  value: number;
  contribution: number;
  direction: Direction;
}

export interface Source {
  document_id: string;
  title: string;
  url: string;
  published_at?: string | null;
  source_type: string;
  source_key: string;
  language_code: string;
  trust_level: TrustLevel;
  summary_ru: string;
  summary_kind: SummaryKind;
  snippet?: string;
  similarity?: number;
}

export interface ResultItemSummary {
  item_id: string;
  rank: number;
  title_ru: string;
  score: number;
  confidence_band: ConfidenceBand;
  key_predictors: Feature[];
  decision_explanation_ru: string;
  narrative_status: NarrativeStatus;
  source_count: number;
}

export interface Provenance {
  llm_provider: string;
  llm_model: string;
  prompt_version: string;
  model_version_id: string;
}

export interface ResultItem extends ResultItemSummary {
  job_id: string;
  query_text: string;
  title_auto: string;
  description_ru: string;
  advantage_ru: string;
  case_example_ru: string;
  case_document_id?: string;
  explanation_ru: string;
  predicted_stage?: number;
  predicted_trend?: number;
  features: Feature[];
  sources: Source[];
  provenance: Provenance;
}

export interface ExcludedCandidate {
  candidate_id: string;
  title_auto: string;
  score: number;
  decision: Exclude<Decision, "WEAK_SIGNAL">;
  decision_reason: string;
  decision_explanation_ru: string;
  document_count: number;
}

export interface ResultStats {
  http_requests_total: number;
  sources_processed: number;
  documents_collected: number;
  candidates_found: number;
  weak_signals_total: number;
  weak_signals_confident: number;
  narratives_generated?: number;
  narratives_fallback?: number;
  model_version_id: string;
  expand_used_fallback?: boolean | null;
}

export interface Results {
  job_id: string;
  query_text: string;
  status: JobStatus;
  items: ResultItemSummary[];
  excluded: ExcludedCandidate[];
  stats: ResultStats;
}

export interface ModelInfo {
  model_version_id: string;
  model_family: string;
  feature_schema_version: string;
  embedding_model: string;
  dataset_version: string;
  trained_at: string;
  metrics: Record<string, number | string>;
  feature_names: string[];
}

export interface ScoreResponse {
  score: number;
  decision: Decision;
  decision_reason: string;
  features: Feature[];
  model_version_id: string;
  predicted_stage?: number;
  predicted_trend?: number;
  enrichment_applied: boolean;
}
