-- collector schema v0001. Владелец: ws_collector.
CREATE TABLE IF NOT EXISTS collector.schema_migrations (
  version     integer PRIMARY KEY,
  name        text NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now()
);

-- Каталог адаптеров-источников (справочник; seed в этой миграции). source_key = enum SourceKey в common.proto.
CREATE TABLE collector.source_catalog (
  source_key         text PRIMARY KEY CHECK (source_key IN ('openalex','arxiv','patentsview','rss','github','hh','wikipedia')),
  display_name       text NOT NULL,
  default_source_type text NOT NULL,
  default_trust      text NOT NULL CHECK (default_trust IN ('HIGH','MEDIUM','LOW')),
  requires_api_key   boolean NOT NULL,
  rate_limit_rps     numeric(6,3) NOT NULL CHECK (rate_limit_rps > 0), -- лимит клиента (не источника), см. §13.4
  enabled            boolean NOT NULL DEFAULT true,
  notes              text
);
INSERT INTO collector.source_catalog VALUES
 ('openalex','OpenAlex (научные публикации)','SCIENTIFIC_PUBLICATION','HIGH',true,5.000,true,'API key обязателен с 2026-02-13'),
 ('arxiv','arXiv (препринты)','PREPRINT','HIGH',false,0.330,true,'1 запрос в 3 с (по документации arXiv API, требует перепроверки)'),
 ('patentsview','PatentsView (патенты USPTO)','PATENT','HIGH',true,1.000,false,'включается после получения ключа; риск R-6'),
 ('rss','RSS-ленты отраслевых медиа','INDUSTRY_MEDIA','MEDIUM',false,2.000,true,'список лент в конфигурации COLLECTOR_RSS_FEEDS'),
 ('github','GitHub Search (репозитории)','CODE_REPOSITORY','MEDIUM',false,0.400,true,'без токена 10 req/min; с токеном 30 req/min (требует перепроверки)'),
 ('hh','hh.ru (вакансии)','VACANCY','MEDIUM',false,1.000,false,'включается после проверки условий API; риск R-6'),
 ('wikipedia','Wikipedia (индикатор зрелости)','ENCYCLOPEDIA','MEDIUM',false,2.000,true,'REST summary + pageviews API');

-- Коллекция = результат одного запуска сбора. Идемпотентность по idempotency_key.
CREATE TABLE collector.collections (
  collection_id      uuid PRIMARY KEY DEFAULT uuidv7(),
  idempotency_key    text NOT NULL UNIQUE CHECK (char_length(idempotency_key) <= 128),
  query_text         text NOT NULL CHECK (char_length(query_text) BETWEEN 2 AND 500),
  mode               text NOT NULL CHECK (mode IN ('SEARCH','ENRICHMENT')),
  terms_ru           text[] NOT NULL DEFAULT '{}',
  terms_en           text[] NOT NULL DEFAULT '{}',
  max_documents_per_source integer NOT NULL CHECK (max_documents_per_source BETWEEN 1 AND 500),
  max_total_documents integer NOT NULL CHECK (max_total_documents BETWEEN 1 AND 3000),
  time_budget_seconds integer NOT NULL CHECK (time_budget_seconds BETWEEN 10 AND 600),
  published_since_year integer NOT NULL CHECK (published_since_year BETWEEN 2000 AND 2100),
  status             text NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED')),
  cancel_requested   boolean NOT NULL DEFAULT false,
  lease_owner        text,
  lease_expires_at   timestamptz,
  documents_total    integer NOT NULL DEFAULT 0 CHECK (documents_total >= 0),
  http_requests_total integer NOT NULL DEFAULT 0 CHECK (http_requests_total >= 0),
  error_code         text,
  error_message      text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  started_at         timestamptz,
  finished_at        timestamptz,
  CHECK (array_length(terms_ru,1) IS NOT NULL OR array_length(terms_en,1) IS NOT NULL),
  CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX collections_queue_idx ON collector.collections (created_at) WHERE status IN ('PENDING','RUNNING');

-- Документ (дедупликация по url_hash; один документ может входить в много коллекций).
CREATE TABLE collector.documents (
  document_id        uuid PRIMARY KEY DEFAULT uuidv7(),
  url_hash           text NOT NULL UNIQUE,            -- sha256(canonical_url)
  url                text NOT NULL CHECK (char_length(url) <= 2048),
  canonical_url      text NOT NULL CHECK (char_length(canonical_url) <= 2048),
  origin_domain      text NOT NULL CHECK (char_length(origin_domain) <= 255),
  title              text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 512),
  body_text          text NOT NULL DEFAULT '' CHECK (char_length(body_text) <= 8000),
  content_hash       text NOT NULL,                   -- sha256(title + body_text) для near-dup первичной проверки
  language_code      text NOT NULL CHECK (language_code ~ '^[a-z]{2,3}$'),
  published_at       timestamptz,                     -- NULL = неизвестна
  source_key         text NOT NULL REFERENCES collector.source_catalog(source_key) ON DELETE RESTRICT ON UPDATE CASCADE,
  source_type        text NOT NULL,
  trust_level        text NOT NULL CHECK (trust_level IN ('HIGH','MEDIUM','LOW')),
  doi                text,
  citation_count     integer CHECK (citation_count IS NULL OR citation_count >= 0),
  engagement_count   integer CHECK (engagement_count IS NULL OR engagement_count >= 0),
  raw_meta           jsonb NOT NULL DEFAULT '{}'::jsonb, -- подмножество ответа адаптера (см. §11.6, J-1); ≤ 8 KB
  fetched_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (pg_column_size(raw_meta) <= 8192)
);
CREATE INDEX documents_source_published_idx ON collector.documents (source_key, published_at DESC);
CREATE INDEX documents_doi_idx ON collector.documents (doi) WHERE doi IS NOT NULL;
CREATE INDEX documents_content_hash_idx ON collector.documents (content_hash);

-- Связь коллекция ↔ документ (M:N). relevance_rank — порядок выдачи адаптера.
CREATE TABLE collector.collection_documents (
  collection_id   uuid NOT NULL REFERENCES collector.collections(collection_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  document_id     uuid NOT NULL REFERENCES collector.documents(document_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  source_key      text NOT NULL,
  relevance_rank  integer NOT NULL CHECK (relevance_rank >= 1),
  matched_term    text NOT NULL,                      -- фраза, по которой найден документ
  PRIMARY KEY (collection_id, document_id)
);
CREATE INDEX collection_documents_doc_idx ON collector.collection_documents (document_id);

-- Запуск адаптера в рамках коллекции (наблюдаемость, статистика «обработано источников»).
CREATE TABLE collector.adapter_runs (
  run_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  collection_id    uuid NOT NULL REFERENCES collector.collections(collection_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  source_key       text NOT NULL REFERENCES collector.source_catalog(source_key) ON DELETE RESTRICT ON UPDATE CASCADE,
  status           text NOT NULL CHECK (status IN ('PENDING','RUNNING','COMPLETED','PARTIAL','FAILED','CANCELLED')),
  http_requests    integer NOT NULL DEFAULT 0 CHECK (http_requests >= 0),
  documents_found  integer NOT NULL DEFAULT 0 CHECK (documents_found >= 0),
  documents_new    integer NOT NULL DEFAULT 0 CHECK (documents_new >= 0),
  error_code       text,
  error_message    text CHECK (error_message IS NULL OR char_length(error_message) <= 500),
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  UNIQUE (collection_id, source_key)
);

-- Кеш проверок Wikipedia (TTL 7 дней, ключ: язык + нормализованное название).
CREATE TABLE collector.encyclopedia_cache (
  language_code    text NOT NULL CHECK (language_code IN ('ru','en')),
  title_norm       text NOT NULL,
  exists_flag      boolean NOT NULL,
  page_url         text,
  pageviews_30d    integer NOT NULL DEFAULT -1,
  page_created_at  timestamptz,
  checked_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (language_code, title_norm)
);

INSERT INTO collector.schema_migrations (version, name) VALUES (1, '0001_init');
