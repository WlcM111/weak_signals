-- insight schema v0003: полные тексты промптов и ответов LLM для повторяемой оценки (аудит A3, режим А; этап 2).
-- Решение владельца проекта 03.10.2026: тексты сохраняются (замена правила §14.5 ТЗ для оценки воспроизводимости).
-- Журнал llm_calls не меняется. replay_key — SHA-256 канонического запроса; correlation_id — job_id задания.
CREATE TABLE insight.llm_recordings (
  recording_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  correlation_id      text NOT NULL DEFAULT '' CHECK (char_length(correlation_id) <= 64),
  purpose             text NOT NULL,
  prompt_version      text NOT NULL,
  request_sha256      text NOT NULL,
  replay_key          text NOT NULL CHECK (char_length(replay_key) = 64),
  provider            text NOT NULL,
  model               text NOT NULL,
  status              text NOT NULL,
  error_code          text,
  error_message       text CHECK (error_message IS NULL OR char_length(error_message) <= 500),
  retry_after_seconds double precision,
  messages            jsonb NOT NULL,
  json_schema_sha256  text NOT NULL DEFAULT '',
  max_tokens          integer NOT NULL,
  temperature         double precision NOT NULL,
  response_text       text,
  prompt_tokens       integer,
  completion_tokens   integer,
  latency_ms          integer NOT NULL DEFAULT 0,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_recordings_correlation_idx ON insight.llm_recordings (correlation_id, recording_id);
