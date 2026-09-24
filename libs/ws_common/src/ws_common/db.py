"""Пул соединений PostgreSQL (psycopg 3 async) с параметрами сессии из ТЗ (§9.3 COMMON)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


async def _configure(conn: AsyncConnection[Any]) -> None:
    """Параметры сессии: search_path задаётся ролью; тайм-ауты — из ТЗ (statement 30 с, lock 5 с)."""
    conn.row_factory = dict_row  # type: ignore[assignment]
    await conn.set_autocommit(True)


def build_pool(
    dsn: str,
    *,
    min_size: int,
    max_size: int,
    statement_timeout_ms: int,
    lock_timeout_ms: int = 5000,
    application_name: str = "weak-signals",
) -> AsyncConnectionPool[AsyncConnection[Any]]:
    """Создаёт (ещё не открытый) пул соединений."""
    options = (
        f"-c statement_timeout={statement_timeout_ms} "
        f"-c lock_timeout={lock_timeout_ms} "
        f"-c application_name={application_name}"
    )
    return AsyncConnectionPool(
        conninfo=make_conninfo(dsn, options=options),
        min_size=min_size,
        max_size=max_size,
        open=False,
        configure=_configure,
        kwargs={"autocommit": True},
    )


@asynccontextmanager
async def transaction(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[AsyncConnection[Any]]:
    """Соединение из пула в явной транзакции (commit при успехе, rollback при исключении)."""
    async with pool.connection() as conn, conn.transaction():
        yield conn


def build_sync_pool(
    dsn: str,
    *,
    min_size: int,
    max_size: int,
    statement_timeout_ms: int,
    lock_timeout_ms: int = 5000,
    application_name: str = "weak-signals",
) -> "ConnectionPool[Connection[Any]]":  # noqa: F821 - имена импортируются лениво, ниже по телу
    """Синхронный пул соединений для сервисов на sync gRPC (ADR-09: analyzer)."""
    from psycopg import Connection  # noqa: PLC0415 - импорт рядом с использованием
    from psycopg_pool import ConnectionPool  # noqa: PLC0415

    options = (
        f"-c statement_timeout={statement_timeout_ms} "
        f"-c lock_timeout={lock_timeout_ms} "
        f"-c application_name={application_name}"
    )

    def configure(conn: "Connection[Any]") -> None:
        """Единый формат строк: словари вместо кортежей."""
        conn.row_factory = dict_row  # type: ignore[assignment]

    return ConnectionPool(
        conninfo=make_conninfo(dsn, options=options),
        min_size=min_size,
        max_size=max_size,
        open=False,
        configure=configure,
        kwargs={"autocommit": True},
    )
