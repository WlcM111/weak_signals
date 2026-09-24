"""PostgreSQL 18 через testcontainers и применение нормативной миграции схемы `orchestrator`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from ws_common.db import build_pool
from ws_common.migrate import apply_migrations

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
ROLE_SQL = """
CREATE ROLE ws_orchestrator LOGIN PASSWORD 'test';
CREATE SCHEMA orchestrator AUTHORIZATION ws_orchestrator;
ALTER ROLE ws_orchestrator SET search_path = orchestrator;
GRANT ALL ON SCHEMA orchestrator TO ws_orchestrator;
"""


@pytest.fixture(scope="session")
def postgres_container() -> PostgresContainer:
    """Контейнер PostgreSQL 18.6 (версия из compose.yaml)."""
    with PostgresContainer("postgres:18.6", dbname="weaksignals") as container:
        yield container


@pytest_asyncio.fixture
async def pool(postgres_container: PostgresContainer) -> AsyncIterator[object]:
    """Пул под ролью-владельцем схемы с применёнными миграциями."""
    import psycopg

    admin_dsn = postgres_container.get_connection_url().replace("postgresql+psycopg2", "postgresql")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS orchestrator CASCADE")
        conn.execute("DROP ROLE IF EXISTS ws_orchestrator")
        for statement in ROLE_SQL.strip().split(";"):
            if statement.strip():
                conn.execute(statement)
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    dsn = f"postgresql://ws_orchestrator:test@{host}:{port}/weaksignals"
    pool = build_pool(dsn, min_size=1, max_size=5, statement_timeout_ms=15000)
    await pool.open(wait=True, timeout=30)
    await apply_migrations(pool, "orchestrator", MIGRATIONS)
    yield pool
    await pool.close()
