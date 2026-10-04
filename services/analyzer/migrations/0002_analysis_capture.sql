-- analyzer schema v0002: запись входов анализа для повторяемой оценки (аудит A3, режим А; этап 2).
-- as_of — дата, на которую считались признаки свежести; vectors — npz с векторами документов и всех остальных
-- вызовов эмбеддера; encyclopedia — ответы Wikipedia по вызовам; funnel — где выбыл каждый документ.
CREATE TABLE analyzer.analysis_captures (
  analysis_id       uuid PRIMARY KEY REFERENCES analyzer.analyses(analysis_id) ON DELETE CASCADE ON UPDATE RESTRICT,
  as_of             timestamptz,
  model_version_id  text NOT NULL,
  embedding_model   text NOT NULL,
  query_text        text NOT NULL,
  params            jsonb NOT NULL,
  config            jsonb NOT NULL,
  funnel            jsonb NOT NULL,
  encyclopedia      jsonb NOT NULL,
  vectors           bytea NOT NULL,
  vectors_sha256    text NOT NULL CHECK (char_length(vectors_sha256) = 64),
  created_at        timestamptz NOT NULL DEFAULT now()
);
