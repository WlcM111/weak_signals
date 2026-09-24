#!/usr/bin/env python3
"""Статическая проверка SQL репозиториев против DDL миграции (без PostgreSQL).

Выявляет расхождения имён таблиц и столбцов между `services/collector/migrations/0001_init.sql`
и запросами адаптеров PostgreSQL до запуска контейнера с БД.

Запуск: python tools/check_sql_conformance.py
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ServiceSchema:
    """Сервис, его схема PostgreSQL и константы со списками столбцов."""

    package: str
    column_constants: dict[str, str]

    @property
    def migration(self) -> Path:
        """Нормативная миграция схемы."""
        return ROOT / "services" / self.package / "migrations" / "0001_init.sql"

    @property
    def postgres_dir(self) -> Path:
        """Каталог репозиториев PostgreSQL."""
        return (
            ROOT / "services" / self.package / "src" / self.package / "adapters" / "outbound" / "postgres"
        )


SERVICES = (
    ServiceSchema(
        package="collector",
        column_constants={
            "COLLECTION_COLUMNS": "collections",
            "DOCUMENT_COLUMNS": "documents",
            "ADAPTER_RUN_COLUMNS": "adapter_runs",
        },
    ),
    ServiceSchema(
        package="orchestrator",
        column_constants={"JOB_COLUMNS": "jobs", "ITEM_COLUMNS": "result_items", "STATS_COLUMNS": "job_stats"},
    ),
    ServiceSchema(
        package="insight",
        column_constants={"_INSIGHT_COLUMNS": "insights"},
    ),
    ServiceSchema(
        package="analyzer",
        column_constants={
            "ANALYSIS_COLUMNS": "analyses",
            "MODEL_VERSION_COLUMNS": "model_versions",
            "CANDIDATE_COLUMNS": "candidates",
        },
    ),
)

_CREATE_TABLE_TEMPLATE = r"CREATE TABLE(?: IF NOT EXISTS)? {schema}\.(\w+)\s*\((.*?)\n\);"
_COLUMN_RE = re.compile(r"^\s{2}(\w+)\s+[a-z]", re.MULTILINE)
_TABLE_USAGE_RE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN)\s+(?:ONLY\s+)?(\w+)(?:\s+AS\s+\w+)?", re.IGNORECASE
)
_INSERT_COLUMNS_RE = re.compile(r"INSERT INTO\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL)
_SET_COLUMN_RE = re.compile(r"(?:SET|,)\s*(\w+)\s*=", re.IGNORECASE)
_SQL_KEYWORDS = frozenset(
    {
        "select", "values", "set", "where", "unnest", "candidate", "victims", "now", "true", "false",
        "skip", "locked",  # конструкция FOR UPDATE SKIP LOCKED
        "ordered", "excluded",  # CTE и псевдотаблица ON CONFLICT
    }
)


def load_schema(service: ServiceSchema) -> dict[str, set[str]]:
    """Таблицы схемы сервиса и их столбцы из нормативной миграции."""
    text = service.migration.read_text(encoding="utf-8")
    pattern = re.compile(
        _CREATE_TABLE_TEMPLATE.format(schema=service.package), re.DOTALL | re.IGNORECASE
    )
    schema: dict[str, set[str]] = {}
    for name, body in pattern.findall(text):
        schema[name] = {
            column
            for column in _COLUMN_RE.findall(body)
            if column.upper() not in {"PRIMARY", "UNIQUE", "CHECK", "CONSTRAINT", "FOREIGN"}
        }
    return schema


def sql_constants(path: Path) -> list[tuple[str, str]]:
    """Строковые константы модуля, похожие на SQL."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", text, re.IGNORECASE):
                found.append((f"{path.name}:{node.lineno}", text))
    return found


def resolve_column_lists(path: Path) -> dict[str, str]:
    """Константы со списками столбцов (`COLLECTION_COLUMNS` и подобные) для подстановки."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value.value, str):
                constants[target.id] = node.value.value
    return constants


def check_service(service: ServiceSchema) -> list[str]:
    """Сверяет таблицы и столбцы, встречающиеся в SQL сервиса, со схемой его миграции."""
    schema = load_schema(service)
    mappers_path = service.postgres_dir / "mappers.py"
    if not mappers_path.is_file():
        # у insight мапперы и SQL живут в одном модуле репозиториев
        mappers_path = service.postgres_dir / "repositories.py"
    mapper_columns = resolve_column_lists(mappers_path)
    problems: list[str] = []
    checked_statements = 0
    for path in sorted(service.postgres_dir.glob("*.py")):
        for location, sql in sql_constants(path):
            checked_statements += 1
            for table in _TABLE_USAGE_RE.findall(sql):
                if table.lower() in _SQL_KEYWORDS or table.lower() in {"information_schema"}:
                    continue
                if table not in schema:
                    problems.append(f"{location}: таблицы {table} нет в схеме {service.package}")
            for table, columns in _INSERT_COLUMNS_RE.findall(sql):
                if table not in schema:
                    continue
                for column in (item.strip() for item in columns.split(",")):
                    if column and column not in schema[table]:
                        problems.append(f"{location}: столбца {table}.{column} нет в схеме")
    # Столбцы, перечисленные в константах mappers.py, должны существовать в своих таблицах.
    # Константа может описывать выборку с join (столбцы с префиксом алиаса, «j.status»): такой
    # столбец ищется среди всех таблиц схемы, потому что алиас не связан с именем таблицы.
    all_columns = {column for columns in schema.values() for column in columns}
    for constant, table in service.column_constants.items():
        listed = [column.strip() for column in mapper_columns.get(constant, "").replace("\n", " ").split(",")]
        for column in listed:
            if not column:
                continue
            if "." in column:
                name = column.split(".", 1)[1]
                if name not in all_columns:
                    problems.append(f"mappers.{constant}: столбца {column} нет ни в одной таблице схемы")
                continue
            if column not in schema.get(table, set()):
                problems.append(f"mappers.{constant}: столбца {table}.{column} нет в схеме")
    print(
        f"{service.package}: таблиц {len(schema)}, проверено SQL-констант {checked_statements}"
    )
    return problems


def main() -> int:
    """Запускает проверку для каждого реализованного сервиса и печатает итог."""
    problems: list[str] = []
    for service in SERVICES:
        if not service.migration.is_file() or not service.postgres_dir.is_dir():
            continue
        problems += check_service(service)
    for problem in problems:
        print(f"ОШИБКА: {problem}")
    print("RESULT: PASS" if not problems else f"RESULT: FAIL ({len(problems)})")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
