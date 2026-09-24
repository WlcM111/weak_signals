-- collector schema v0002: лимит клиента Wikipedia 2 → 10 запросов в секунду.
-- Проверка одного названия — до трёх запросов (summary, pageviews, revisions); при 2 запросах/с пачка
-- из 20 названий занимала до 30 с и не укладывалась в дедлайн вызова CheckEncyclopedia (20 с).
-- Лимит Wikimedia REST API для клиентов с User-Agent — 200 запросов/с, значение 10 остаётся вежливым.
UPDATE collector.source_catalog
SET rate_limit_rps = 10.000,
    notes = 'REST summary + pageviews API; лимит клиента 10 req/s (v0002)'
WHERE source_key = 'wikipedia';