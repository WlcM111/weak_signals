-- collector schema v0007: источник «Поисковая платформа» Роспатента (патенты РФ, СНГ и мирового фонда).
-- API: POST https://searchplatform.rospatent.gov.ru/patsearch/v0.2/search, JWT-ключ в заголовке Authorization
-- (ключи выдаются зарегистрированным пользователям платформы). Тип PATENT, доверенность HIGH.
-- Лимит частоты в документации не опубликован: 0.5 запроса в секунду — вежливый клиент.
-- Откат: DELETE FROM collector.source_catalog WHERE source_key = 'rospatent'; и прежний CHECK из 0006.
ALTER TABLE collector.source_catalog DROP CONSTRAINT IF EXISTS source_catalog_source_key_check;
ALTER TABLE collector.source_catalog ADD CONSTRAINT source_catalog_source_key_check
  CHECK (source_key IN ('openalex','arxiv','patentsview','rss','github','hh','wikipedia','semantic_scholar','zenodo','gdelt','rospatent'));
INSERT INTO collector.source_catalog
  (source_key, display_name, default_source_type, default_trust, requires_api_key, rate_limit_rps, enabled, notes)
VALUES
  ('rospatent', 'Роспатент (патенты РФ, СНГ и мирового фонда)', 'PATENT', 'HIGH', true, 0.500, true,
   'Поисковая платформа, API v0.2, POST /search: запрос qn, фильтр date_published; ключ WS_ROSPATENT_TOKEN')
ON CONFLICT (source_key) DO NOTHING;
