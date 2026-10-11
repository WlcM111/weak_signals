-- collector schema v0014 (этап 4.6, аудит 11.10.2026: P07–P13, P21): даты и полнота архива новостей,
-- возобновляемый перечень Wayback, инкрементальный OAI-PMH, локальный научный поиск, реестр лент.
-- Откат: обратные ALTER/DROP для перечисленных колонок, таблицы feed_registry и строки каталога 'corpus'.
ALTER TABLE collector.source_catalog DROP CONSTRAINT IF EXISTS source_catalog_source_key_check;
ALTER TABLE collector.source_catalog ADD CONSTRAINT source_catalog_source_key_check
  CHECK (source_key IN ('openalex','arxiv','patentsview','rss','github','hh','wikipedia','semantic_scholar','zenodo','gdelt',
                        'rospatent','hackernews','news_archive','crossref','huggingface','corpus'));
INSERT INTO collector.source_catalog
  (source_key, display_name, default_source_type, default_trust, requires_api_key, rate_limit_rps, enabled, notes)
VALUES
  ('corpus', 'Корпус arXiv (локальный поиск)', 'PREPRINT', 'HIGH', false, 100.000, true,
   'локальный корпус метаданных arXiv (OAI-PMH): заголовки и аннотации; внешних запросов при сборе нет')
ON CONFLICT (source_key) DO NOTHING;

-- P09: дата публикации отдельно от даты сохранения; P08: качество текста записи.
ALTER TABLE collector.news_archive ADD COLUMN IF NOT EXISTS published_source text NOT NULL DEFAULT 'unknown';
ALTER TABLE collector.news_archive ADD COLUMN IF NOT EXISTS text_status text NOT NULL DEFAULT 'slug';
UPDATE collector.news_archive SET
  published_source = CASE
    WHEN origin = 'rss' THEN 'rss'
    WHEN url ~ '/20[0-9]{2}/[0-9]{2}(/|$)' OR url ~ '-20[0-9]{2}-[0-9]{2}-[0-9]{2}/?$' OR url ~ '/news/home/20[0-9]{6}'
      THEN 'url'
    ELSE 'capture' END,
  text_status = CASE WHEN origin = 'rss' THEN 'rss' ELSE 'slug' END
WHERE published_source = 'unknown';
UPDATE collector.news_archive SET published_on = NULL WHERE published_source = 'capture';
ALTER TABLE collector.news_archive DROP CONSTRAINT IF EXISTS news_archive_published_source_check;
ALTER TABLE collector.news_archive ADD CONSTRAINT news_archive_published_source_check
  CHECK (published_source IN ('url', 'rss', 'page', 'capture', 'unknown'));
ALTER TABLE collector.news_archive DROP CONSTRAINT IF EXISTS news_archive_text_status_check;
ALTER TABLE collector.news_archive ADD CONSTRAINT news_archive_text_status_check
  CHECK (text_status IN ('slug', 'rss', 'page', 'failed'));
CREATE INDEX IF NOT EXISTS news_archive_text_status ON collector.news_archive (text_status);

-- P11: «partial» — месяц перечислен не до конца; resume — «номер префикса|ключ продолжения CDX».
ALTER TABLE collector.news_archive_harvest DROP CONSTRAINT IF EXISTS news_archive_harvest_status_check;
ALTER TABLE collector.news_archive_harvest ADD CONSTRAINT news_archive_harvest_status_check
  CHECK (status IN ('done', 'failed', 'partial'));
ALTER TABLE collector.news_archive_harvest ADD COLUMN IF NOT EXISTS resume text;

-- P12: отметка последнего изменения метаданных OAI-PMH (datestamp) для инкрементального обновления.
ALTER TABLE collector.corpus_harvest ADD COLUMN IF NOT EXISTS watermark date;

-- Ресурсы 02_weak_signals_sources: найденные ленты RSS/Atom официальных страниц ресурсов и доменов эталона.
CREATE TABLE IF NOT EXISTS collector.feed_registry (
  feed_url     text PRIMARY KEY CHECK (length(feed_url) <= 2048),
  source_id    text NOT NULL,
  site_url     text NOT NULL,
  title        text NOT NULL DEFAULT '',
  status       text NOT NULL CHECK (status IN ('active', 'failed')),
  entries      integer NOT NULL DEFAULT 0 CHECK (entries >= 0),
  detail       text NOT NULL DEFAULT '',
  checked_at   timestamptz NOT NULL DEFAULT now(),
  last_success timestamptz
);
