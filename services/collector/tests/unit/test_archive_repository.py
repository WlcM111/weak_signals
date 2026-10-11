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
        archive = PostgresNewsArchive(_Pool([{"period": "2026-09", "status": "partial", "resume": "0|k"}]))  # type: ignore[arg-type]
        self.assertEqual(await archive.harvest_states("techcrunch.com"), {"2026-09": ("partial", "0|k")})
        pool = _Pool([{"title": "Robot skill marketplace", "published_on": date(2026, 9, 4)}])
        archive = PostgresNewsArchive(pool)  # type: ignore[arg-type]
        self.assertEqual(await archive.titles_matching(["robot", "skill"], 2, date(2025, 1, 1), date(2026, 10, 1), 10),
                         [("Robot skill marketplace", date(2026, 9, 4))])
        sql, params = pool.cursor.executed[0]
        self.assertEqual(params, (["robot", "skill"], ["robot", "skill"], 2, date(2025, 1, 1), date(2026, 10, 1),
                                  None, None, 10))  # этап 4.5: происхождение записей (None — любое)
        self.assertIn("tokens && %s::text[]", sql)  # этап 4.4: хотя бы одно слово через GIN, затем порог совпадений
        self.assertNotIn("@>", sql)

    async def test_event_candidates_query_and_rows(self) -> None:
        pool = _Pool([{"url": "https://techcrunch.com/2026/08/11/x/", "publisher": "techcrunch.com", "origin": "wayback",
                       "published_on": date(2026, 8, 11), "captured_at": None, "title": "Tetragen raises seed",
                       "summary": "", "tokens": ["tetragen", "raises", "seed", "welding"]}])
        rows = await PostgresNewsArchive(pool).event_candidates(["welding"], ["raises"], 1, date(2024, 10, 1), 50)  # type: ignore[arg-type]
        sql, params = pool.cursor.executed[0]
        self.assertEqual(params, (["welding"], ["raises"], ["welding"], 1, date(2024, 10, 1), ["welding"], 50))
        self.assertEqual(sql.count("tokens && %s::text[]"), 2)  # словарь и событие стартапа
        self.assertEqual((rows[0][0].title, rows[0][1]), ("Tetragen raises seed", ("tetragen", "raises", "seed", "welding")))

    async def test_archive_search_ranks_by_matched_words(self) -> None:
        pool = _Pool([{"url": "https://techcrunch.com/a/", "publisher": "techcrunch.com", "origin": "wayback",
                       "published_on": date(2025, 3, 1), "captured_at": None, "title": "A", "summary": ""}])
        entries = await PostgresNewsArchive(pool).search(["kv", "cache"], date(2024, 10, 1), 5, 2)  # type: ignore[arg-type]
        sql, params = pool.cursor.executed[0]
        self.assertEqual(params, (["kv", "cache"], ["kv", "cache"], 2, date(2024, 10, 1), date(2024, 10, 1),
                                  ["kv", "cache"], 5))  # этап 4.6 (P09): окно по дате публикации или сохранения
        self.assertLess(sql.index("INTERSECT"), sql.index("published_on DESC"))  # сначала число совпавших слов
        self.assertEqual(entries[0].url, "https://techcrunch.com/a/")

    async def test_corpus_reads_rows_by_column_name(self) -> None:
        corpus = PostgresCorpus(_Pool([{"title": "Optical circuit switching", "created_on": date(2025, 6, 1)}]))  # type: ignore[arg-type]
        self.assertEqual(await corpus.titles_matching(["optical", "switching"], 2, date(2021, 1, 1), date(2024, 10, 1), 5),
                         [("Optical circuit switching", date(2025, 6, 1))])
        corpus = PostgresCorpus(_Pool([{"set_spec": "cs", "resumption_token": "t", "records": 1000, "kept": 400,
                                        "status": "running", "watermark": date(2026, 10, 1)}]))  # type: ignore[arg-type]
        self.assertEqual(await corpus.harvest_state("cs"),
                         CorpusHarvestState("cs", "t", 1000, 400, "running", date(2026, 10, 1)))


if __name__ == "__main__":
    unittest.main()
