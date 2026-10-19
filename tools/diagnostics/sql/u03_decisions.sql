-- U-03: решения analyzer по кандидатам последних заданий; MATURE с пометкой «Также:» — зрелость при нехватке источников.
WITH recent AS (
  SELECT j.analysis_id, left(q.query_text, 32) AS topic
  FROM orchestrator.jobs j JOIN orchestrator.queries q USING (query_id)
  WHERE j.analysis_id IS NOT NULL
  ORDER BY j.created_at DESC LIMIT :n
)
SELECT r.topic, c.decision, c.decision_reason, count(*) AS candidates,
       count(*) FILTER (WHERE c.decision_explanation_ru LIKE '%Также:%') AS with_evidence_note
FROM recent r JOIN analyzer.candidates c ON c.analysis_id = r.analysis_id
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC;
