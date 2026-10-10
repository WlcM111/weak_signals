-- collector schema v0013 (этап 4.3): локальный корпус метаданных arXiv (OAI-PMH, наборы cs, eess, q-fin).
-- Корпус — источник названий технологий: словосочетания заголовков с быстрым ростом числа работ в 2024–2026 годах.
-- tokens — слова заголовка и начала аннотации (поиск работ по фразам темы); created_on — дата первой версии.
-- corpus_harvest — состояние выгрузки набора: токен продолжения OAI-PMH (возобновляемая загрузка).
-- Откат: DROP TABLE collector.corpus_harvest, collector.corpus_papers;
CREATE TABLE IF NOT EXISTS collector.corpus_papers (
  paper_id    text PRIMARY KEY,
  title       text NOT NULL,
  abstract    text NOT NULL DEFAULT '',
  created_on  date NOT NULL,
  categories  text[] NOT NULL DEFAULT '{}',
  tokens      text[] NOT NULL,
  inserted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS corpus_papers_tokens_gin ON collector.corpus_papers USING gin (tokens);
CREATE INDEX IF NOT EXISTS corpus_papers_created_on ON collector.corpus_papers (created_on);

CREATE TABLE IF NOT EXISTS collector.corpus_harvest (
  set_spec         text PRIMARY KEY,
  resumption_token text,
  records          integer NOT NULL DEFAULT 0 CHECK (records >= 0),
  kept             integer NOT NULL DEFAULT 0 CHECK (kept >= 0),
  status           text NOT NULL CHECK (status IN ('running', 'done', 'failed')),
  detail           text NOT NULL DEFAULT '',
  updated_at       timestamptz NOT NULL DEFAULT now()
);
