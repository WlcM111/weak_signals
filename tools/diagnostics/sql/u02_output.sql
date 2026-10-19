-- U-02: карточки выдачи по основанию (только научные/патентные источники против рынка) и исключённые по причинам.
WITH recent AS (
  SELECT j.job_id, left(q.query_text, 32) AS topic
  FROM orchestrator.jobs j JOIN orchestrator.queries q USING (query_id)
  ORDER BY j.created_at DESC LIMIT :n
), kinds AS (
  SELECT r.topic, i.item_id,
         bool_or(s.source_type IN ('NEWS','INDUSTRY_MEDIA','PRESS_RELEASE','ANALYTICAL_REPORT','CORPORATE_BLOG','GOVERNMENT')) AS market,
         bool_and(s.source_type = 'PATENT') AS patent_only
  FROM recent r JOIN orchestrator.result_items i ON i.job_id = r.job_id
  JOIN orchestrator.result_item_sources s ON s.item_id = i.item_id
  GROUP BY r.topic, i.item_id
)
SELECT topic, count(*) AS cards, count(*) FILTER (WHERE market) AS market,
       count(*) FILTER (WHERE NOT market AND patent_only) AS patent,
       count(*) FILTER (WHERE NOT market AND NOT patent_only) AS research
FROM kinds GROUP BY topic ORDER BY topic;
