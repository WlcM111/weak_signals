-- collector schema v0008: трасса сбора для повторяемой оценки (аудит A3, режим А; этап 2).
-- raw_responses — итог каждого логического запроса адаптера: адрес без секретов, код отказа или сжатое тело ответа.
-- document_events — судьба каждого документа, выданного адаптером: принят, дубль, лимит источника, отбракован.
-- run_id различает попытки одного сбора (повтор после потери аренды пишется отдельно).
CREATE TABLE collector.raw_responses (
  response_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  collection_id  uuid NOT NULL REFERENCES collector.collections(collection_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  run_id         text NOT NULL CHECK (char_length(run_id) <= 64),
  seq            integer NOT NULL CHECK (seq >= 1),
  source_key     text NOT NULL,
  method         text NOT NULL CHECK (method IN ('GET','POST')),
  url            text NOT NULL,
  request_body   jsonb,
  outcome        text NOT NULL,
  error_message  text CHECK (error_message IS NULL OR char_length(error_message) <= 500),
  body_gzip      bytea,
  body_sha256    text,
  body_bytes     integer NOT NULL DEFAULT 0 CHECK (body_bytes >= 0),
  started_at     timestamptz NOT NULL,
  duration_ms    integer NOT NULL DEFAULT 0 CHECK (duration_ms >= 0),
  CHECK ((outcome = 'OK') = (body_gzip IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS raw_responses_collection_idx ON collector.raw_responses (collection_id, run_id, seq);

CREATE TABLE collector.document_events (
  event_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  collection_id  uuid NOT NULL REFERENCES collector.collections(collection_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  run_id         text NOT NULL CHECK (char_length(run_id) <= 64),
  seq            integer NOT NULL CHECK (seq >= 1),
  source_key     text NOT NULL,
  matched_term   text NOT NULL,
  event          text NOT NULL CHECK (event IN ('ACCEPTED','DEDUP','SOURCE_CAP','REJECTED')),
  url            text NOT NULL,
  url_hash       text,
  doi            text,
  title          text NOT NULL,
  detail         text CHECK (detail IS NULL OR char_length(detail) <= 500)
);
CREATE INDEX IF NOT EXISTS document_events_collection_idx ON collector.document_events (collection_id, run_id, seq);
