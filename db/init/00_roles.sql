-- Роли, база и схемы кластера PostgreSQL 18 (одна физическая БД weaksignals, четыре схемы, четыре роли).
-- Скрипт идемпотентен. При первом старте на пустом томе его выполняет docker-entrypoint-initdb.d (как .sql,
-- через psql — исполняемые права и shell-обёртка не нужны); на существующем томе его можно выполнить
-- повторно командой `make db-roles`, чтобы привести роли и пароли к текущему .env без потери данных.
-- Пароли читаются из переменных окружения контейнера postgres командой psql \getenv (psql ≥ 15).
-- Роль каждого сервиса владеет только своей схемой; прав на чужие схемы нет (проверка: §11.10 ARCHITECTURE_TZ.md).
\getenv WS_ORCHESTRATOR_DB_PASSWORD WS_ORCHESTRATOR_DB_PASSWORD
\getenv WS_COLLECTOR_DB_PASSWORD WS_COLLECTOR_DB_PASSWORD
\getenv WS_ANALYZER_DB_PASSWORD WS_ANALYZER_DB_PASSWORD
\getenv WS_INSIGHT_DB_PASSWORD WS_INSIGHT_DB_PASSWORD
SELECT format('CREATE ROLE %I LOGIN', role_name)
FROM unnest(ARRAY['ws_orchestrator', 'ws_collector', 'ws_analyzer', 'ws_insight']) AS role_name
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name)
\gexec
ALTER ROLE ws_orchestrator LOGIN PASSWORD :'WS_ORCHESTRATOR_DB_PASSWORD';
ALTER ROLE ws_collector    LOGIN PASSWORD :'WS_COLLECTOR_DB_PASSWORD';
ALTER ROLE ws_analyzer     LOGIN PASSWORD :'WS_ANALYZER_DB_PASSWORD';
ALTER ROLE ws_insight      LOGIN PASSWORD :'WS_INSIGHT_DB_PASSWORD';
SELECT 'CREATE DATABASE weaksignals OWNER postgres ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'weaksignals')
\gexec
\connect weaksignals
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS orchestrator AUTHORIZATION ws_orchestrator;
CREATE SCHEMA IF NOT EXISTS collector    AUTHORIZATION ws_collector;
CREATE SCHEMA IF NOT EXISTS analyzer     AUTHORIZATION ws_analyzer;
CREATE SCHEMA IF NOT EXISTS insight      AUTHORIZATION ws_insight;
ALTER ROLE ws_orchestrator SET search_path = orchestrator;
ALTER ROLE ws_collector    SET search_path = collector;
ALTER ROLE ws_analyzer     SET search_path = analyzer;
ALTER ROLE ws_insight      SET search_path = insight;
-- Лимиты соединений на роль (см. §13.6): суммарно < max_connections=200.
ALTER ROLE ws_orchestrator CONNECTION LIMIT 60;
ALTER ROLE ws_collector    CONNECTION LIMIT 40;
ALTER ROLE ws_analyzer     CONNECTION LIMIT 30;
ALTER ROLE ws_insight      CONNECTION LIMIT 30;