-- orchestrator schema v0001. Владелец: ws_orchestrator. Применяется runner-ом ws_common.migrate (таблица schema_migrations).
CREATE TABLE IF NOT EXISTS orchestrator.schema_migrations (
  version     integer PRIMARY KEY,
  name        text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

-- Запрос пользователя (единица входа). Один запрос → ровно одно задание (jobs.query_id UNIQUE).
CREATE TABLE orchestrator.queries (
  query_id         uuid PRIMARY KEY DEFAULT uuidv7(),
  query_text       text NOT NULL CHECK (char_length(query_text) BETWEEN 2 AND 500),
  normalized_text  text NOT NULL,                       -- lower + trim + collapse spaces; для кеша/дедупликации
  requested_top_n  integer NOT NULL DEFAULT 15 CHECK (requested_top_n BETWEEN 1 AND 50),
  client_ip_hash   text,                                -- sha256(ip+salt) для rate limiting и аудита; NULL если неизвестен
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX queries_normalized_text_idx ON orchestrator.queries (normalized_text, created_at DESC);

-- Задание (длительная операция). Статусы и переходы: §9.2. Аренда: lease_owner/lease_expires_at.
CREATE TABLE orchestrator.jobs (
  job_id             uuid PRIMARY KEY DEFAULT uuidv7(),
  query_id           uuid NOT NULL UNIQUE REFERENCES orchestrator.queries(query_id) ON DELETE RESTRICT ON UPDATE RESTRICT,
  status             text NOT NULL DEFAULT 'QUEUED'
                     CHECK (status IN ('QUEUED','COLLECTING','ANALYZING','NARRATING','COMPLETED','PARTIAL','FAILED','CANCELLED')),
  attempt            integer NOT NULL DEFAULT 0 CHECK (attempt >= 0 AND attempt <= 3),
  cancel_requested   boolean NOT NULL DEFAULT false,
  lease_owner        text,                              -- идентификатор воркера (hostname:pid:uuid)
  lease_expires_at   timestamptz,
  collection_id      uuid,                              -- ссылка на collector.collections (без FK: другой владелец)
  analysis_id        uuid,                              -- ссылка на analyzer.analyses (без FK)
  error_code         text,
  error_message      text CHECK (error_message IS NULL OR char_length(error_message) <= 2000),
  created_at         timestamptz NOT NULL DEFAULT now(),
  started_at         timestamptz,
  finished_at        timestamptz,
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
  CHECK (status NOT IN ('COMPLETED','PARTIAL','FAILED','CANCELLED') OR finished_at IS NOT NULL)
);
-- Очередь: выбор доступных заданий (QUEUED или просроченная аренда) — частичный индекс под запрос воркера.
CREATE INDEX jobs_queue_idx ON orchestrator.jobs (created_at)
  WHERE status IN ('QUEUED','COLLECTING','ANALYZING','NARRATING');
CREATE INDEX jobs_status_created_idx ON orchestrator.jobs (status, created_at DESC);

-- Журнал переходов (аудит/диагностика). Только вставка.
CREATE TABLE orchestrator.job_events (
  event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id       uuid NOT NULL REFERENCES orchestrator.jobs(job_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  occurred_at  timestamptz NOT NULL DEFAULT now(),
  from_status  text,
  to_status    text NOT NULL,
  worker_id    text,
  detail       text CHECK (detail IS NULL OR char_length(detail) <= 2000)
);
CREATE INDEX job_events_job_idx ON orchestrator.job_events (job_id, event_id);

-- Статистика задания (1:1 с jobs). Отдельная таблица: заполняется по стадиям, не участвует в аренде.
CREATE TABLE orchestrator.job_stats (
  job_id                   uuid PRIMARY KEY REFERENCES orchestrator.jobs(job_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  http_requests_total      integer NOT NULL DEFAULT 0 CHECK (http_requests_total >= 0),   -- «обработано источников»
  sources_processed        integer NOT NULL DEFAULT 0 CHECK (sources_processed >= 0),     -- адаптеров завершено
  documents_collected      integer NOT NULL DEFAULT 0 CHECK (documents_collected >= 0),
  candidates_found         integer NOT NULL DEFAULT 0 CHECK (candidates_found >= 0),      -- кандидатов на слабый сигнал
  weak_signals_total       integer NOT NULL DEFAULT 0 CHECK (weak_signals_total >= 0),
  weak_signals_confident   integer NOT NULL DEFAULT 0 CHECK (weak_signals_confident >= 0),-- score >= 0.75
  collect_ms               integer CHECK (collect_ms IS NULL OR collect_ms >= 0),
  analyze_ms               integer CHECK (analyze_ms IS NULL OR analyze_ms >= 0),
  narrate_ms               integer CHECK (narrate_ms IS NULL OR narrate_ms >= 0),
  narratives_generated     integer NOT NULL DEFAULT 0 CHECK (narratives_generated >= 0),
  narratives_fallback      integer NOT NULL DEFAULT 0 CHECK (narratives_fallback >= 0),
  model_version_id         text,
  expand_used_fallback     boolean
);

-- Элемент итоговой выдачи (ТОП-N). Снимок-отчёт: копирует поля из analyzer/insight осознанно (§11.5, денормализация D-1).
CREATE TABLE orchestrator.result_items (
  item_id                uuid PRIMARY KEY DEFAULT uuidv7(),
  job_id                 uuid NOT NULL REFERENCES orchestrator.jobs(job_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  rank                   integer NOT NULL CHECK (rank >= 1),
  candidate_id           uuid NOT NULL,                              -- ссылка на analyzer.candidates (без FK)
  title_ru               text NOT NULL CHECK (char_length(title_ru) <= 200),
  title_auto             text NOT NULL CHECK (char_length(title_auto) <= 200), -- метка кластера analyzer
  score                  numeric(5,4) NOT NULL CHECK (score >= 0 AND score <= 1),
  decision_reason        text NOT NULL,
  decision_explanation_ru text NOT NULL,
  description_ru         text NOT NULL,
  advantage_ru           text NOT NULL,
  case_example_ru        text NOT NULL,
  case_document_id       uuid,                                       -- ссылка на collector.documents (без FK)
  explanation_ru         text NOT NULL,
  narrative_status       text NOT NULL CHECK (narrative_status IN ('GENERATED','FALLBACK_EXTRACTIVE')),
  llm_provider           text NOT NULL,
  llm_model              text NOT NULL,
  prompt_version         text NOT NULL,
  predicted_stage        smallint CHECK (predicted_stage BETWEEN 1 AND 4),
  predicted_trend        smallint CHECK (predicted_trend BETWEEN 1 AND 3),
  document_count         integer NOT NULL CHECK (document_count >= 0),
  created_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (job_id, rank),
  UNIQUE (job_id, candidate_id)
);

-- Ключевые предикторы элемента (все признаки, чтобы UI показывал полный разбор). PK составной; feature_name — ключ реестра.
CREATE TABLE orchestrator.result_item_features (
  item_id        uuid NOT NULL REFERENCES orchestrator.result_items(item_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  feature_name   text NOT NULL CHECK (char_length(feature_name) <= 64),
  label_ru       text NOT NULL,
  value          double precision NOT NULL,
  contribution   double precision NOT NULL,
  direction      text NOT NULL CHECK (direction IN ('supports_weak_signal','supports_mature','neutral')),
  display_order  integer NOT NULL CHECK (display_order >= 1),
  PRIMARY KEY (item_id, feature_name),
  UNIQUE (item_id, display_order)
);

-- Источники элемента (ТЗ: наименование, ссылка, дата, тип, язык, доверенность, резюме и его вид).
CREATE TABLE orchestrator.result_item_sources (
  item_id         uuid NOT NULL REFERENCES orchestrator.result_items(item_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  position        integer NOT NULL CHECK (position >= 1),
  document_id     uuid NOT NULL,                     -- ссылка на collector.documents (без FK)
  title           text NOT NULL,
  url             text NOT NULL CHECK (char_length(url) <= 2048),
  published_at    timestamptz,                       -- NULL = дата неизвестна
  source_type     text NOT NULL,
  source_key      text NOT NULL,
  language_code   text NOT NULL CHECK (language_code ~ '^[a-z]{2,3}$'),
  trust_level     text NOT NULL CHECK (trust_level IN ('HIGH','MEDIUM','LOW')),
  summary_ru      text NOT NULL,
  summary_kind    text NOT NULL CHECK (summary_kind IN ('ORIGINAL_RU','GENERATIVE_SUMMARY','EXTRACTIVE')),
  snippet         text NOT NULL,
  similarity      real NOT NULL CHECK (similarity >= 0 AND similarity <= 1),
  PRIMARY KEY (item_id, position),
  UNIQUE (item_id, document_id)
);

-- Исключённые кандидаты с причинами (ТЗ: причины исключения зрелых технологий / нерелевантных кандидатов).
CREATE TABLE orchestrator.excluded_candidates (
  job_id               uuid NOT NULL REFERENCES orchestrator.jobs(job_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  candidate_id         uuid NOT NULL,
  title_auto           text NOT NULL,
  score                numeric(5,4) NOT NULL CHECK (score >= 0 AND score <= 1),
  decision             text NOT NULL CHECK (decision IN ('MATURE','HYPE_OR_NOISE','INSUFFICIENT_EVIDENCE','OFF_TOPIC')),
  decision_reason      text NOT NULL,
  decision_explanation_ru text NOT NULL,
  document_count       integer NOT NULL CHECK (document_count >= 0),
  PRIMARY KEY (job_id, candidate_id)
);

-- Ключи идемпотентности HTTP (POST /api/v1/queries). Срок жизни 24 ч (удаление фоновой задачей).
CREATE TABLE orchestrator.idempotency_keys (
  idempotency_key  text PRIMARY KEY CHECK (char_length(idempotency_key) BETWEEN 8 AND 128),
  request_hash     text NOT NULL,                  -- sha256 канонизированного тела запроса
  job_id           uuid NOT NULL REFERENCES orchestrator.jobs(job_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  created_at       timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL
);
CREATE INDEX idempotency_keys_expires_idx ON orchestrator.idempotency_keys (expires_at);

INSERT INTO orchestrator.schema_migrations (version, name) VALUES (1, '0001_init');
