-- collector schema v0003: источник Semantic Scholar (Graph API /paper/search).
-- Индексирует журнальные публикации и препринты, включая arXiv, поэтому страхует arXiv, который
-- ограничивает частоту на пограничном прокси. Ключ необязателен: с ключом лимит 1 запрос в секунду,
-- без ключа — общий пул анонимных клиентов. Лимит клиента 0.5 запроса в секунду вежлив в обоих случаях.
ALTER TABLE collector.source_catalog DROP CONSTRAINT IF EXISTS source_catalog_source_key_check;
ALTER TABLE collector.source_catalog ADD CONSTRAINT source_catalog_source_key_check
  CHECK (source_key IN ('openalex','arxiv','patentsview','rss','github','hh','wikipedia','semantic_scholar'));
INSERT INTO collector.source_catalog
  (source_key, display_name, default_source_type, default_trust, requires_api_key, rate_limit_rps, enabled, notes)
VALUES
  ('semantic_scholar', 'Semantic Scholar (научные публикации и препринты)', 'SCIENTIFIC_PUBLICATION', 'HIGH',
   false, 0.500, true, 'Graph API /paper/search; ключ необязателен (x-api-key, 1 req/s)')
ON CONFLICT (source_key) DO NOTHING;