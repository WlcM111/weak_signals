"""Поднятие PostgreSQL 18 и применение нормативной миграции схемы `analyzer`.

Синхронный вариант фикстур: сервис использует sync-стек (ADR-09), поэтому и пул синхронный.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from testcontainers.postgres import PostgresContainer

from ws_common.db import build_sync_pool
from ws_common.migrate import apply_migrations_sync

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
ROLE_SQL = """
CREATE ROLE ws_analyzer LOGIN PASSWORD 'test';
CREATE SCHEMA analyzer AUTHORIZATION ws_analyzer;
ALTER ROLE ws_analyzer SET search_path = analyzer;
GRANT ALL ON SCHEMA analyzer TO ws_analyzer;
"""


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Контейнер PostgreSQL 18.6 (версия из compose.yaml)."""
    with PostgresContainer("postgres:18.6", dbname="weaksignals") as container:
        yield container


@pytest.fixture
def pool(postgres_container: PostgresContainer):  # noqa: ANN201 - фикстура pytest
    """Пул соединений под ролью-владельцем схемы с применёнными миграциями."""
    import psycopg

    admin_dsn = postgres_container.get_connection_url().replace("postgresql+psycopg2", "postgresql")
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS analyzer CASCADE")
        conn.execute("DROP ROLE IF EXISTS ws_analyzer")
        for statement in ROLE_SQL.strip().split(";"):
            if statement.strip():
                conn.execute(statement)
    host = postgres_container.get_container_host_ip()
    port = postgres_container.get_exposed_port(5432)
    dsn = f"postgresql://ws_analyzer:test@{host}:{port}/weaksignals"
    pool = build_sync_pool(dsn, min_size=1, max_size=4, statement_timeout_ms=15000)
    pool.open(wait=True, timeout=30)
    apply_migrations_sync(pool, "analyzer", MIGRATIONS)
    yield pool
    pool.close()
