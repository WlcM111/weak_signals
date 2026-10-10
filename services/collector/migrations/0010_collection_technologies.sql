-- collector schema v0010 (этап 4): технологии прицельного прохода сбора.
-- terms_technologies — технологии от модели (ExpandQuery, prompt expand_v2), technologies_mined — извлечённые
-- из документов широкого прохода (статистика по заголовкам, без LLM). По обоим спискам идёт прицельный поиск.
-- Откат: ALTER TABLE collector.collections DROP COLUMN terms_technologies, DROP COLUMN technologies_mined;
ALTER TABLE collector.collections
  ADD COLUMN IF NOT EXISTS terms_technologies text[] NOT NULL DEFAULT '{}',
  ADD COLUMN IF NOT EXISTS technologies_mined text[] NOT NULL DEFAULT '{}';
