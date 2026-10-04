-- orchestrator schema v0003: трасса задания по стадиям (аудит A3, режим А; этап 2).
-- job — параметры и сроки; expand — фразы расширения; collect/analyze — итог сбора и анализа;
-- narrate — пул, вердикты рубрики, вероятности классификатора, раунды доводки, отказы и показанные карточки.
CREATE TABLE orchestrator.job_traces (
  job_id      uuid NOT NULL REFERENCES orchestrator.jobs(job_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  stage       text NOT NULL CHECK (char_length(stage) BETWEEN 1 AND 32),
  payload     jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, stage)
);
