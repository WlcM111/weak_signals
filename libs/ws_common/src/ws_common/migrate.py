"""Применение SQL-миграций при старте сервиса (§11.9 ТЗ).

Правила: только вперёд; файлы `NNNN_<name>.sql`; сериализация через `pg_advisory_lock(hashtext('<schema>'))`;
каждая миграция — в своей транзакции; версия фиксируется в `<schema>.schema_migrations`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True, slots=True)
class Migration:
    """Файл миграции: номер версии, имя и SQL."""

    version: int
    name: str
    sql: str


def discover(directory: Path) -> list[Migration]:
    """Читает и сортирует миграции каталога; отвергает файлы с некорректным именем."""
    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILE_RE.match(path.name)
        if match is None:
            raise ValueError(f"недопустимое имя миграции: {path.name}")
        migrations.append(Migration(int(match.group(1)), match.group(2), path.read_text(encoding="utf-8")))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise ValueError("дублирующиеся номера миграций")
    return migrations


async def _applied_versions(conn: AsyncConnection[Any], schema: str) -> set[int]:
    """Список применённых версий; пустое множество, если таблицы ещё нет."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = 'schema_migrations')",
            (schema,),
        )
        row = await cur.fetchone()
        exists = bool(row["exists"]) if isinstance(row, dict) else bool(row and row[0])
        if not exists:
            return set()
        await cur.execute(f'SELECT version FROM "{schema}".schema_migrations')  # noqa: S608 - schema из конфигурации
        rows = await cur.fetchall()
    return {int(r["version"]) if isinstance(r, dict) else int(r[0]) for r in rows}


async def apply_migrations(
    pool: AsyncConnectionPool[AsyncConnection[Any]], schema: str, directory: Path
) -> list[int]:
    """Применяет недостающие миграции; возвращает список применённых версий (повторный запуск — пустой)."""
    migrations = discover(directory)
    applied: list[int] = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (schema,))
        try:
            known = await _applied_versions(conn, schema)
            for migration in migrations:
                if migration.version in known:
                    continue
                async with conn.transaction(), conn.cursor() as cur:
                    await cur.execute(migration.sql)  # type: ignore[arg-type]
                    await cur.execute(
                        f'INSERT INTO "{schema}".schema_migrations (version, name) '  # noqa: S608 - schema из конфигурации
                        "VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                        (migration.version, migration.name),
                    )
                applied.append(migration.version)
        finally:
            async with conn.cursor() as cur:
                await cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (schema,))
    return applied


def apply_migrations_sync(pool: Any, schema: str, directory: Path) -> list[int]:
    """Синхронный вариант применения миграций (для сервисов на sync-стеке)."""
    migrations = discover(directory)
    applied: list[int] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (schema,))
        try:
            known = _applied_versions_sync(conn, schema)
            for migration in migrations:
                if migration.version in known:
                    continue
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute(migration.sql)
                    cur.execute(
                        f'INSERT INTO "{schema}".schema_migrations (version, name) '  # noqa: S608
                        "VALUES (%s, %s) ON CONFLICT (version) DO NOTHING",
                        (migration.version, migration.name),
                    )
                applied.append(migration.version)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (schema,))
    return applied


def _applied_versions_sync(conn: Any, schema: str) -> set[int]:
    """Применённые версии в синхронном соединении."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = 'schema_migrations')",
            (schema,),
        )
        row = cur.fetchone()
        exists = bool(row["exists"]) if isinstance(row, dict) else bool(row and row[0])
        if not exists:
            return set()
        cur.execute(f'SELECT version FROM "{schema}".schema_migrations')  # noqa: S608
        rows = cur.fetchall()
    return {int(r["version"]) if isinstance(r, dict) else int(r[0]) for r in rows}
