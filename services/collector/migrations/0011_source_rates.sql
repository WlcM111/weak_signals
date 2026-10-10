-- collector schema v0011 (этап 4.2): частоты источников по опубликованным правилам сервисов (аудит API 07.10.2026).
-- GitHub Search: 10 запросов в минуту без токена, 30 — с токеном; в каталоге — значение для токена (27/мин с запасом),
-- без токена код снижает до 1 запроса в 7 с. Wikipedia: анонимный бот с контактом в User-Agent — 200 в минуту,
-- прежние 10 запросов/с (600/мин) правилу не соответствовали.
-- Откат: UPDATE collector.source_catalog SET rate_limit_rps = 0.400 WHERE source_key = 'github';
--        UPDATE collector.source_catalog SET rate_limit_rps = 10.000 WHERE source_key = 'wikipedia';
UPDATE collector.source_catalog
   SET rate_limit_rps = 0.450,
       notes = 'Search API: 30 запросов/мин с токеном, 10 без токена (тогда код ограничивает 1 запросом в 7 с)'
 WHERE source_key = 'github';
UPDATE collector.source_catalog
   SET rate_limit_rps = 2.000,
       notes = 'Wikimedia: 200 запросов/мин для анонимного бота с контактом в User-Agent'
 WHERE source_key = 'wikipedia';
