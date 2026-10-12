-- collector schema v0015 (этап 5.0, аудит раунда 3 и round2): качество и происхождение текста документов (F15, F16, N11),
-- повторные попытки обогащения и страницы-заглушки (F11, F12), записи из карт сайтов (N16), приоритет сборов (N08),
-- отчёт об отборе технологий (F06), расписание фоновых загрузок (N15).
ALTER TABLE collector.documents ADD COLUMN IF NOT EXISTS text_quality text NOT NULL DEFAULT 'unknown';
ALTER TABLE collector.documents ADD COLUMN IF NOT EXISTS published_source text NOT NULL DEFAULT 'unknown';
ALTER TABLE collector.documents DROP CONSTRAINT IF EXISTS documents_text_quality_check;
ALTER TABLE collector.documents ADD CONSTRAINT documents_text_quality_check
  CHECK (text_quality IN ('title', 'abstract', 'article', 'blocked', 'unknown'));
ALTER TABLE collector.documents DROP CONSTRAINT IF EXISTS documents_published_source_check;
ALTER TABLE collector.documents ADD CONSTRAINT documents_published_source_check
  CHECK (published_source IN ('api', 'page', 'rss', 'sitemap', 'url', 'capture', 'unknown'));

ALTER TABLE collector.news_archive ADD COLUMN IF NOT EXISTS enrich_attempts integer NOT NULL DEFAULT 0;
ALTER TABLE collector.news_archive ADD COLUMN IF NOT EXISTS next_retry_at timestamptz;
ALTER TABLE collector.news_archive DROP CONSTRAINT IF EXISTS news_archive_text_status_check;
ALTER TABLE collector.news_archive ADD CONSTRAINT news_archive_text_status_check
  CHECK (text_status IN ('slug', 'rss', 'page', 'failed', 'blocked', 'retry'));
ALTER TABLE collector.news_archive DROP CONSTRAINT IF EXISTS news_archive_origin_check;
ALTER TABLE collector.news_archive ADD CONSTRAINT news_archive_origin_check CHECK (origin IN ('wayback', 'rss', 'sitemap'));
ALTER TABLE collector.news_archive DROP CONSTRAINT IF EXISTS news_archive_published_source_check;
ALTER TABLE collector.news_archive ADD CONSTRAINT news_archive_published_source_check
  CHECK (published_source IN ('url', 'rss', 'page', 'sitemap', 'capture', 'unknown'));
CREATE INDEX IF NOT EXISTS news_archive_retry ON collector.news_archive (next_retry_at) WHERE text_status = 'retry';

ALTER TABLE collector.collections ADD COLUMN IF NOT EXISTS selection_report jsonb;

CREATE TABLE IF NOT EXISTS collector.archive_schedule (
  job         text PRIMARY KEY,
  last_run    timestamptz NOT NULL,
  last_status text NOT NULL,
  detail      text NOT NULL DEFAULT ''
);
