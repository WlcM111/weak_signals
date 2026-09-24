-- collector schema v0004: источник Zenodo (REST API поиска записей, только препринты).
-- Поиск Zenodo ограничен 30 запросами в минуту на клиента; лимит клиента 0.3 запроса в секунду
-- (18 в минуту) оставляет запас. Тип PREPRINT: доверенность документа определяется типом
-- (type_trust в trust_rules.yaml), поэтому default_trust совпадает с доверенностью препринтов.
ALTER TABLE collector.source_catalog DROP CONSTRAINT IF EXISTS source_catalog_source_key_check;
ALTER TABLE collector.source_catalog ADD CONSTRAINT source_catalog_source_key_check
  CHECK (source_key IN ('openalex','arxiv','patentsview','rss','github','hh','wikipedia','semantic_scholar','zenodo'));
INSERT INTO collector.source_catalog
  (source_key, display_name, default_source_type, default_trust, requires_api_key, rate_limit_rps, enabled, notes)
VALUES
  ('zenodo', 'Zenodo (препринты)', 'PREPRINT', 'HIGH', false, 0.300, true,
   'REST API /api/records: subtype=Preprint, sort=mostrecent, 25 записей на страницу без ключа')
ON CONFLICT (source_key) DO NOTHING;