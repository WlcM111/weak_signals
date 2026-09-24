-- insight schema v0001. Владелец: ws_insight.
CREATE TABLE IF NOT EXISTS insight.schema_migrations (
  version     integer PRIMARY KEY,
  name        text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

-- Версии промптов (seed из кода при старте: insight_v1, expand_v1). template_sha256 — хеш файла шаблона.
CREATE TABLE insight.prompt_versions (
  prompt_version   text PRIMARY KEY CHECK (prompt_version ~ '^[a-z]+_v[0-9]+$'),
  purpose          text NOT NULL CHECK (purpose IN ('insight','expand')),
  template_sha256  text NOT NULL,
  output_schema_version text NOT NULL,   -- напр. "insight_llm_output.schema.json@1"
  created_at       timestamptz NOT NULL DEFAULT now(),
  description      text
);

-- Инсайт (результат GenerateInsight). Идемпотентность по idempotency_key; кеш по input_hash.
CREATE TABLE insight.insights (
  insight_id        uuid PRIMARY KEY DEFAULT uuidv7(),
  idempotency_key   text NOT NULL UNIQUE CHECK (char_length(idempotency_key) <= 160),
  input_hash        text NOT NULL,                     -- sha256(canonical(candidate + evidence ids + prompt_version))
  candidate_id      uuid NOT NULL,                     -- ссылка на analyzer.candidates (без FK)
  prompt_version    text NOT NULL REFERENCES insight.prompt_versions(prompt_version) ON DELETE RESTRICT ON UPDATE RESTRICT,
  status            text NOT NULL CHECK (status IN ('GENERATED','FALLBACK_EXTRACTIVE')),
  provider          text NOT NULL CHECK (provider IN ('gigachat','yandexgpt','local_llamacpp','none')),
  model             text NOT NULL DEFAULT '',
  title_ru          text NOT NULL CHECK (char_length(title_ru) <= 200),
  description_ru    text NOT NULL CHECK (char_length(description_ru) <= 800),
  advantage_ru      text NOT NULL CHECK (char_length(advantage_ru) <= 600),
  case_example_ru   text NOT NULL CHECK (char_length(case_example_ru) <= 600),
  case_document_id  uuid,
  explanation_ru    text NOT NULL CHECK (char_length(explanation_ru) <= 800),
  grounding_passed  boolean NOT NULL,
  unsupported_numbers integer NOT NULL DEFAULT 0 CHECK (unsupported_numbers >= 0),
  unknown_document_refs integer NOT NULL DEFAULT 0 CHECK (unknown_document_refs >= 0),
  features_mentioned integer NOT NULL DEFAULT 0 CHECK (features_mentioned >= 0),
  attempts          integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX insights_input_hash_idx ON insight.insights (input_hash, created_at DESC);

-- Резюме источников инсайта.
CREATE TABLE insight.insight_sources (
  insight_id     uuid NOT NULL REFERENCES insight.insights(insight_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  document_id    uuid NOT NULL,
  position       integer NOT NULL CHECK (position >= 1),
  summary_ru     text NOT NULL CHECK (char_length(summary_ru) <= 400),
  summary_kind   text NOT NULL CHECK (summary_kind IN ('ORIGINAL_RU','GENERATIVE_SUMMARY','EXTRACTIVE')),
  PRIMARY KEY (insight_id, document_id),
  UNIQUE (insight_id, position)
);

-- Кеш расширения запросов (ExpandQuery). TTL 7 дней.
CREATE TABLE insight.query_expansions (
  query_norm       text PRIMARY KEY,
  prompt_version   text NOT NULL REFERENCES insight.prompt_versions(prompt_version) ON DELETE RESTRICT ON UPDATE RESTRICT,
  ru_terms         text[] NOT NULL CHECK (array_length(ru_terms,1) BETWEEN 1 AND 8),
  en_terms         text[] NOT NULL CHECK (array_length(en_terms,1) BETWEEN 1 AND 8),
  domain_tags      text[] NOT NULL DEFAULT '{}',
  used_fallback    boolean NOT NULL,
  provider         text NOT NULL,
  model            text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now()
);

-- Журнал вызовов LLM (ТЗ: явное логирование выбора модели). Промпты и ответы НЕ сохраняются (§14.5); только метаданные.
CREATE TABLE insight.llm_calls (
  call_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purpose            text NOT NULL CHECK (purpose IN ('insight','expand','healthcheck')),
  idempotency_key    text,
  provider           text NOT NULL,
  model              text NOT NULL,
  prompt_version     text NOT NULL,
  request_sha256     text NOT NULL,
  prompt_tokens      integer CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
  completion_tokens  integer CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
  latency_ms         integer NOT NULL CHECK (latency_ms >= 0),
  status             text NOT NULL CHECK (status IN ('OK','SCHEMA_REJECTED','GROUNDING_REJECTED','RATE_LIMITED','TIMEOUT','PROVIDER_ERROR','BLOCKED')),
  error_code         text,
  created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_calls_created_idx ON insight.llm_calls (created_at DESC);

INSERT INTO insight.schema_migrations (version, name) VALUES (1, '0001_init');
