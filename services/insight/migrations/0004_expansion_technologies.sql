-- insight schema v0004 (этап 4): список конкретных технологий в расширении запроса (prompt expand_v2).
-- Технологии задают прицельный поиск collector; пустой массив — резервное расширение без LLM или старая версия промпта.
-- Откат: ALTER TABLE insight.query_expansions DROP COLUMN technologies;
ALTER TABLE insight.query_expansions
  ADD COLUMN IF NOT EXISTS technologies text[] NOT NULL DEFAULT '{}'
  CHECK (coalesce(array_length(technologies, 1), 0) <= 40);
