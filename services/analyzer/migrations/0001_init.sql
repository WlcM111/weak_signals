-- analyzer schema v0001. Владелец: ws_analyzer.
CREATE TABLE IF NOT EXISTS analyzer.schema_migrations (
  version     integer PRIMARY KEY,
  name        text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

-- Реестр версий модели (артефакт лежит в volume model-store; строка создаётся analyzer-ом при регистрации манифеста).
CREATE TABLE analyzer.model_versions (
  model_version_id        text PRIMARY KEY CHECK (model_version_id ~ '^wsclf-[0-9]{4}\.[0-9]{2}\.[0-9]{2}-[0-9]+$'),
  model_family            text NOT NULL CHECK (model_family IN ('logreg_elasticnet','lightgbm')),
  feature_schema_version  text NOT NULL,           -- напр. "v1"
  embedding_model         text NOT NULL,           -- напр. "intfloat/multilingual-e5-base"
  dataset_version         text NOT NULL,           -- напр. "ds-2026.09.20-v1"
  artifact_path           text NOT NULL,           -- относительный путь в model-store
  artifact_sha256         text NOT NULL,
  git_commit              text,
  trained_at              timestamptz NOT NULL,
  threshold               double precision NOT NULL CHECK (threshold > 0 AND threshold < 1),
  accuracy                double precision NOT NULL CHECK (accuracy BETWEEN 0 AND 1),
  precision_score         double precision NOT NULL CHECK (precision_score BETWEEN 0 AND 1),
  recall_score            double precision NOT NULL CHECK (recall_score BETWEEN 0 AND 1),
  f1_score                double precision NOT NULL CHECK (f1_score BETWEEN 0 AND 1),
  roc_auc                 double precision NOT NULL CHECK (roc_auc BETWEEN 0 AND 1),
  test_size               integer NOT NULL CHECK (test_size > 0),
  evaluation_protocol     text NOT NULL,
  stage_model_present     boolean NOT NULL DEFAULT false,
  trend_model_present     boolean NOT NULL DEFAULT false,
  is_active               boolean NOT NULL DEFAULT false,
  registered_at           timestamptz NOT NULL DEFAULT now()
);
-- Ровно одна активная модель.
CREATE UNIQUE INDEX model_versions_one_active_idx ON analyzer.model_versions ((is_active)) WHERE is_active;

-- Анализ = обработка одной коллекции одной версией модели. Идемпотентность по idempotency_key.
CREATE TABLE analyzer.analyses (
  analysis_id            uuid PRIMARY KEY DEFAULT uuidv7(),
  idempotency_key        text NOT NULL UNIQUE CHECK (char_length(idempotency_key) <= 128),
  collection_id          uuid NOT NULL,              -- ссылка на collector.collections (без FK)
  query_text             text NOT NULL CHECK (char_length(query_text) BETWEEN 2 AND 500),
  model_version_id       text NOT NULL REFERENCES analyzer.model_versions(model_version_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  top_n                  integer NOT NULL CHECK (top_n BETWEEN 1 AND 50),
  max_candidates         integer NOT NULL CHECK (max_candidates BETWEEN 5 AND 100),
  weak_signal_threshold  double precision NOT NULL CHECK (weak_signal_threshold > 0 AND weak_signal_threshold < 1),
  min_evidence_documents integer NOT NULL CHECK (min_evidence_documents >= 1),
  status                 text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')),
  cancel_requested       boolean NOT NULL DEFAULT false,
  lease_owner            text,
  lease_expires_at       timestamptz,
  documents_input        integer NOT NULL DEFAULT 0,
  documents_after_dedup  integer NOT NULL DEFAULT 0,
  clusters_total         integer NOT NULL DEFAULT 0,
  candidates_scored      integer NOT NULL DEFAULT 0,
  weak_signals_total     integer NOT NULL DEFAULT 0,
  weak_signals_confident integer NOT NULL DEFAULT 0,
  excluded_mature        integer NOT NULL DEFAULT 0,
  excluded_hype_or_noise integer NOT NULL DEFAULT 0,
  excluded_insufficient_evidence integer NOT NULL DEFAULT 0,
  excluded_off_topic     integer NOT NULL DEFAULT 0,
  duration_ms            integer,
  error_code             text,
  error_message          text,
  created_at             timestamptz NOT NULL DEFAULT now(),
  started_at             timestamptz,
  finished_at            timestamptz,
  CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX analyses_queue_idx ON analyzer.analyses (created_at) WHERE status IN ('PENDING','RUNNING');

-- Кандидат = кластер документов коллекции с решением и скорингом.
CREATE TABLE analyzer.candidates (
  candidate_id            uuid PRIMARY KEY DEFAULT uuidv7(),
  analysis_id             uuid NOT NULL REFERENCES analyzer.analyses(analysis_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  cluster_index           integer NOT NULL CHECK (cluster_index >= 0),
  rank                    integer NOT NULL DEFAULT 0 CHECK (rank >= 0),      -- 0 = исключён
  title_auto              text NOT NULL CHECK (char_length(title_auto) BETWEEN 1 AND 200),
  keyphrases              text[] NOT NULL CHECK (array_length(keyphrases,1) BETWEEN 1 AND 10),
  score                   double precision NOT NULL CHECK (score BETWEEN 0 AND 1),
  decision                text NOT NULL CHECK (decision IN ('WEAK_SIGNAL','MATURE','HYPE_OR_NOISE','INSUFFICIENT_EVIDENCE','OFF_TOPIC')),
  decision_reason         text NOT NULL CHECK (decision_reason IN ('MODEL_SCORE','ENCYCLOPEDIA_MATURE','MARKET_LEADERS','MATURITY_LEXICON',
                                                                  'MARKETING_DOMINANT','HYPE_LEXICON','NO_TRUSTED_SOURCE','SINGLE_SOURCE','LOW_QUERY_RELEVANCE')),
  decision_explanation_ru text NOT NULL CHECK (char_length(decision_explanation_ru) <= 500),
  document_count          integer NOT NULL CHECK (document_count >= 1),
  query_relevance         double precision NOT NULL CHECK (query_relevance BETWEEN 0 AND 1),
  predicted_stage         smallint CHECK (predicted_stage BETWEEN 1 AND 4),
  predicted_trend         smallint CHECK (predicted_trend BETWEEN 1 AND 3),
  UNIQUE (analysis_id, cluster_index),
  CHECK ((decision = 'WEAK_SIGNAL' AND rank >= 1) OR (decision <> 'WEAK_SIGNAL' AND rank = 0))
);
CREATE UNIQUE INDEX candidates_rank_idx ON analyzer.candidates (analysis_id, rank) WHERE rank >= 1;

-- Значения признаков и вклады (EAV по реестру признаков; обоснование §11.6, E-1).
CREATE TABLE analyzer.candidate_features (
  candidate_id    uuid NOT NULL REFERENCES analyzer.candidates(candidate_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  feature_name    text NOT NULL CHECK (char_length(feature_name) <= 64),
  value           double precision NOT NULL,
  contribution    double precision NOT NULL,
  PRIMARY KEY (candidate_id, feature_name)
);

-- Документы кластера (доказательства). document_id — ссылка на collector.documents (без FK).
CREATE TABLE analyzer.candidate_documents (
  candidate_id    uuid NOT NULL REFERENCES analyzer.candidates(candidate_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  document_id     uuid NOT NULL,
  similarity      real NOT NULL CHECK (similarity BETWEEN 0 AND 1),
  is_evidence     boolean NOT NULL DEFAULT false,      -- входит в top-8 доказательств
  snippet         text CHECK (snippet IS NULL OR char_length(snippet) <= 600),
  source_type     text NOT NULL,
  trust_level     text NOT NULL CHECK (trust_level IN ('HIGH','MEDIUM','LOW')),
  published_year  integer,
  PRIMARY KEY (candidate_id, document_id)
);
CREATE INDEX candidate_documents_evidence_idx ON analyzer.candidate_documents (candidate_id, similarity DESC) WHERE is_evidence;

-- Кеш эмбеддингов документов (без pgvector: поиск по векторам в БД не выполняется; обоснование §11.7).
CREATE TABLE analyzer.document_embeddings (
  document_id      uuid NOT NULL,
  embedding_model  text NOT NULL,
  content_hash     text NOT NULL,
  dims             smallint NOT NULL CHECK (dims IN (384, 768, 1024)),
  vector           real[] NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (document_id, embedding_model),
  CHECK (cardinality(vector) = dims)
);

-- Кеш признаков зрелости по Wikipedia на уровне кандидата хранится в candidate_features (wiki_exists, wiki_pageviews_30d_log).

INSERT INTO analyzer.schema_migrations (version, name) VALUES (1, '0001_init');
