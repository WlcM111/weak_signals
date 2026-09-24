"""Поднятие PostgreSQL 18 и применение нормативной миграции схемы `collector`."""

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
CREATE ROLE ws_collector LOGIN PASSWORD 'test';
CREATE SCHEMA collector AUTHORIZATION ws_collector;
ALTER ROLE ws_collector SET search_path = collector;
GRANT ALL ON SCHEMA collector TO ws_collector;
"""


@pytest.fixture(scope="session")
def postgres_container() -> AsyncIterator[PostgresContainer]:
    """Контейнер PostgreSQL 18.6 (версия из compose.yaml)."""
    with PostgresContainer("postgres:18.6", dbname="weaksignals") as container:
        yield container


@pytest_asyncio.fixture
async def pool(postgres_container: PostgresContainer):  # noqa: ANN201 - фикстура pytest
    """Пул соединений под ролью-владельцем схемы с применёнными миграциями."""
    import psycopg

    admin_dsn = postgres_container.get_connection_url().replace("postgresql+psycopg2", "postgresql")
    async with await psycopg.AsyncConnection.connect(admin_dsn, autocommit=True) as conn:
        # Контейнер живёт всю сессию, а фикстура вызывается на каждый тест: схему нужно
        # пересоздавать, иначе второй тест падает на DuplicateSchema (это отдельный класс
        # исключения, DuplicateObject его не перехватывает).
        await conn.execute("DROP SCHEMA IF EXISTS collector CASCADE")
        for statement in ROLE_SQL.strip().split(";"):
            if statement.strip():
                try:
                    await conn.execute(statement)
                except (psycopg.errors.DuplicateObject, psycopg.errors.DuplicateSchema):
                    pass
    dsn = (
        f"host={postgres_container.get_container_host_ip()} "
        f"port={postgres_container.get_exposed_port(5432)} "
        "dbname=weaksignals user=ws_collector password=test"
    )
    pool = build_pool(dsn, min_size=1, max_size=4, statement_timeout_ms=30_000)
    await pool.open(wait=True, timeout=30)
    await apply_migrations(pool, "collector", MIGRATIONS)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE collections, documents, collection_documents, adapter_runs CASCADE")
        await conn.execute("TRUNCATE encyclopedia_cache")
    yield pool
    await pool.close()
