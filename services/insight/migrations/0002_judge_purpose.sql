-- insight schema v0002: назначение вызова LLM «judge» — смысловая оценка кандидатов перед нарративом.
ALTER TABLE insight.prompt_versions DROP CONSTRAINT IF EXISTS prompt_versions_purpose_check;
ALTER TABLE insight.prompt_versions ADD CONSTRAINT prompt_versions_purpose_check
  CHECK (purpose IN ('insight','expand','judge'));
ALTER TABLE insight.llm_calls DROP CONSTRAINT IF EXISTS llm_calls_purpose_check;
ALTER TABLE insight.llm_calls ADD CONSTRAINT llm_calls_purpose_check
  CHECK (purpose IN ('insight','expand','healthcheck','judge'));
