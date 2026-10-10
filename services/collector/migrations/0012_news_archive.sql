-- collector schema v0012 (этап 4.3): локальный архив новостей и новые источники Crossref, Hugging Face Hub.
-- news_archive — адреса статей изданий из Wayback Machine (заголовок — слова адреса) и все записи RSS-лент
-- (настоящий заголовок и анонс). Сбор ищет по словам (tokens) без внешних запросов во время задания.
-- news_archive_harvest — какие месяцы каких изданий уже перечислены в Wayback (возобновляемая загрузка).
-- Откат: DROP TABLE collector.news_archive_harvest, collector.news_archive;
--        DELETE FROM collector.source_catalog WHERE source_key IN ('news_archive','crossref','huggingface');
--        ALTER TABLE collector.collections DROP COLUMN technologies_emerging; и прежний CHECK из 0009.
ALTER TABLE collector.source_catalog DROP CONSTRAINT IF EXISTS source_catalog_source_key_check;
ALTER TABLE collector.source_catalog ADD CONSTRAINT source_catalog_source_key_check
  CHECK (source_key IN ('openalex','arxiv','patentsview','rss','github','hh','wikipedia','semantic_scholar','zenodo','gdelt',
                        'rospatent','hackernews','news_archive','crossref','huggingface'));
INSERT INTO collector.source_catalog
  (source_key, display_name, default_source_type, default_trust, requires_api_key, rate_limit_rps, enabled, notes)
VALUES
  ('news_archive', 'Архив новостей (Wayback Machine, RSS)', 'NEWS', 'MEDIUM', false, 100.000, true,
   'локальный индекс адресов статей изданий (Wayback Machine) и записей RSS; внешних запросов при сборе нет'),
  ('crossref', 'Crossref', 'SCIENTIFIC_PUBLICATION', 'HIGH', false, 2.000, true,
   'api.crossref.org/works: query.bibliographic, from-pub-date; без ключа, mailto для вежливого пула'),
  ('huggingface', 'Hugging Face Hub', 'CODE_REPOSITORY', 'MEDIUM', false, 1.000, true,
   'huggingface.co/api/models и /api/datasets: поиск по названию')
ON CONFLICT (source_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS collector.news_archive (
  url          text PRIMARY KEY CHECK (length(url) <= 2048),
  publisher    text NOT NULL,
  origin       text NOT NULL CHECK (origin IN ('wayback', 'rss')),
  published_on date,
  captured_at  timestamptz,
  title        text NOT NULL CHECK (length(title) BETWEEN 1 AND 512),
  summary      text NOT NULL DEFAULT '',
  tokens       text[] NOT NULL,
  inserted_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS news_archive_tokens_gin ON collector.news_archive USING gin (tokens);
CREATE INDEX IF NOT EXISTS news_archive_published_on ON collector.news_archive (published_on DESC);

CREATE TABLE IF NOT EXISTS collector.news_archive_harvest (
  publisher   text NOT NULL,
  period      text NOT NULL CHECK (period ~ '^[0-9]{4}-[0-9]{2}$'),
  status      text NOT NULL CHECK (status IN ('done', 'failed')),
  rows        integer NOT NULL DEFAULT 0 CHECK (rows >= 0),
  detail      text NOT NULL DEFAULT '',
  finished_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (publisher, period)
);

-- Технологии из корпусов (оценка возникновения) — рядом с технологиями модели и извлечёнными (0010).
ALTER TABLE collector.collections ADD COLUMN IF NOT EXISTS technologies_emerging text[] NOT NULL DEFAULT '{}';
