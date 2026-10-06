-- collector schema v0009: источник Hacker News Search (Algolia) — второй новостной поиск вместо GDELT (этап 3.1).
-- API: GET https://hn.algolia.com/api/v1/search (без ключа), фильтр дат numericFilters=created_at_i>…,created_at_i<…;
-- лимит 10 000 запросов в час с одного IP (документация hn.algolia.com/api), при использовании — «Search by Algolia».
-- 1 запрос в секунду — вежливый клиент с большим запасом до лимита.
-- Откат: DELETE FROM collector.source_catalog WHERE source_key = 'hackernews'; и прежний CHECK из 0007.
ALTER TABLE collector.source_catalog DROP CONSTRAINT IF EXISTS source_catalog_source_key_check;
ALTER TABLE collector.source_catalog ADD CONSTRAINT source_catalog_source_key_check
  CHECK (source_key IN ('openalex','arxiv','patentsview','rss','github','hh','wikipedia','semantic_scholar','zenodo','gdelt','rospatent','hackernews'));
INSERT INTO collector.source_catalog
  (source_key, display_name, default_source_type, default_trust, requires_api_key, rate_limit_rps, enabled, notes)
VALUES
  ('hackernews', 'Hacker News (Search by Algolia)', 'INDUSTRY_MEDIA', 'MEDIUM', false, 1.000, true,
   'hn.algolia.com/api/v1/search: tags=story, окна дат created_at_i; без ключа; 10 000 запросов в час с IP')
ON CONFLICT (source_key) DO NOTHING;
