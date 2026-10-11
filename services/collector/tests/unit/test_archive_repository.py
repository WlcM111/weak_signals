"""Этап 4.3.1: чтение архива и корпуса — строки пула приходят словарями (ws_common.db: row_factory = dict_row).

В 4.3 три метода обращались к строкам по номеру колонки и падали с KeyError: 0 на первой же строке: загрузка
Wayback останавливалась на первом издании, а оценка возникновения в сборе молча оставалась пустой.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import date

if importlib.util.find_spec("psycopg") is None:  # окружение без драйвера: заглушки только для импорта модуля
    _psycopg, _rows, _pool = types.ModuleType("psycopg"), types.ModuleType("psycopg.rows"), types.ModuleType("psycopg_pool")
    _psycopg.AsyncConnection = object
    _rows.dict_row = object()
    _psycopg.rows = _rows
    _pool.AsyncConnectionPool = object
    sys.modules.update({"psycopg": _psycopg, "psycopg.rows": _rows, "psycopg_pool": _pool})

from collector.adapters.outbound.postgres.archive_repository import PostgresCorpus, PostgresNewsArchive  # noqa: E402
from collector.domain.archive import CorpusHarvestState  # noqa: E402


class _Cursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows, self.executed = rows, []

    async def __aenter__(self) -> "_Cursor":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: object = None) -> None:
        self.executed.append((sql, params))

    async def executemany(self, sql: str, rows: object) -> None:
        self.executed.append((sql, rows))

    async def fetchall(self) -> list[dict]:
        return list(self.rows)

    async def fetchone(self) -> dict | None:
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> "_Connection":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def cursor(self, row_factory: object = None) -> _Cursor:
        return self._cursor


class _Pool:
    """Как пул проекта: каждая строка — словарь колонок, обращение по номеру невозможно."""

    def __init__(self, rows: list[dict]) -> None:
        self.cursor = _Cursor(rows)

    def connection(self) -> _Connection:
        return _Connection(self.cursor)


class ArchiveRepositoryRowsTest(unittest.IsolatedAsyncioTestCase):
    async def test_archive_reads_rows_by_column_name(self) -> None:
        archive = PostgresNewsArchive(_Pool([{"period": "2026-09", "status": "done"}]))  # type: ignore[arg-type]
        self.assertEqual(await archive.harvested_periods("techcrunch.com"), {"2026-09": "done"})
        pool = _Pool([{"title": "Robot skill marketplace", "published_on": date(2026, 9, 4)}])
        archive = PostgresNewsArchive(pool)  # type: ignore[arg-type]
        self.assertEqual(await archive.titles_for(["robot"], date(2025, 1, 1), 10),
                         [("Robot skill marketplace", date(2026, 9, 4))])
        self.assertEqual(pool.cursor.executed[0][1], (["robot"], date(2025, 1, 1), 10))

    async def test_corpus_reads_rows_by_column_name(self) -> None:
        corpus = PostgresCorpus(_Pool([{"title": "Optical circuit switching", "created_on": date(2025, 6, 1)}]))  # type: ignore[arg-type]
        self.assertEqual(await corpus.titles_for(["optical"], date(2021, 1, 1), 5),
                         [("Optical circuit switching", date(2025, 6, 1))])
        corpus = PostgresCorpus(_Pool([{"set_spec": "cs", "resumption_token": "t", "records": 1000, "kept": 400,
                                        "status": "running"}]))  # type: ignore[arg-type]
        self.assertEqual(await corpus.harvest_state("cs"), CorpusHarvestState("cs", "t", 1000, 400, "running"))


if __name__ == "__main__":
    unittest.main()
