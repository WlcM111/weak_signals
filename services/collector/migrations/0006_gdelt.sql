-- collector schema v0006: источник GDELT DOC 2.0 (мировые новости о пилотах, контрактах, финансировании).
-- Деловые сигналы из датасета организаторов (страхование роботов, маркетплейсы навыков, роботы как услуга)
-- появляются в новостях раньше, чем в научных статьях. Тип INDUSTRY_MEDIA, доверенность MEDIUM: новость
-- подсказывает сигнал, но не подтверждает его в одиночку. Лимит 0.2 запроса в секунду — один запрос в 5 с.
-- Откат: DELETE FROM collector.source_catalog WHERE source_key = 'gdelt'; и прежний CHECK из 0004.
ALTER TABLE collector.source_catalog DROP CONSTRAINT IF EXISTS source_catalog_source_key_check;
ALTER TABLE collector.source_catalog ADD CONSTRAINT source_catalog_source_key_check
  CHECK (source_key IN ('openalex','arxiv','patentsview','rss','github','hh','wikipedia','semantic_scholar','zenodo','gdelt'));
INSERT INTO collector.source_catalog
  (source_key, display_name, default_source_type, default_trust, requires_api_key, rate_limit_rps, enabled, notes)
VALUES
  ('gdelt', 'GDELT (мировые новости)', 'INDUSTRY_MEDIA', 'MEDIUM', false, 0.200, true,
   'DOC 2.0 API, режим artlist: фраза темы и деловые маркеры, английские источники, окно 3 месяца')
ON CONFLICT (source_key) DO NOTHING;
