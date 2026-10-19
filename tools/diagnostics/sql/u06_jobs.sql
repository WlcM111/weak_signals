-- U-06: статус и причина неполноты последних заданий (N задаётся переменной psql :n).
SELECT j.job_id, left(q.query_text, 48) AS topic, j.status, coalesce(j.error_code, '') AS error_code,
       left(coalesce(j.error_message, ''), 200) AS reason, j.collection_id, j.analysis_id, j.created_at
FROM orchestrator.jobs j JOIN orchestrator.queries q USING (query_id)
ORDER BY j.created_at DESC LIMIT :n;
