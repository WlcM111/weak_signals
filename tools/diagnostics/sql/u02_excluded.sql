WITH recent AS (
  SELECT j.job_id, left(q.query_text, 32) AS topic
  FROM orchestrator.jobs j JOIN orchestrator.queries q USING (query_id)
  ORDER BY j.created_at DESC LIMIT :n
)
SELECT r.topic, e.decision, e.decision_reason, count(*) AS excluded
FROM recent r JOIN orchestrator.excluded_candidates e ON e.job_id = r.job_id
GROUP BY 1, 2, 3 ORDER BY 1, 4 DESC;
