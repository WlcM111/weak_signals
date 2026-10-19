-- U-06: исход каждого источника в сборах последних заданий: какой источник сделал сбор PARTIAL и почему.
WITH recent AS (
  SELECT j.job_id, left(q.query_text, 32) AS topic, j.collection_id
  FROM orchestrator.jobs j JOIN orchestrator.queries q USING (query_id)
  ORDER BY j.created_at DESC LIMIT :n
)
SELECT r.topic, a.source_key, a.status, coalesce(a.error_code, '') AS error_code,
       left(coalesce(a.error_message, ''), 160) AS error_message, a.documents_found, a.documents_new
FROM recent r JOIN collector.adapter_runs a ON a.collection_id = r.collection_id
WHERE a.status <> 'COMPLETED'
ORDER BY r.topic, a.source_key;
