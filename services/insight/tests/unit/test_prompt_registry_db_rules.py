"""Версии и назначения промптов совместимы с ограничениями таблицы insight.prompt_versions.

Регистрация промптов идёт при старте insight; нарушение ограничения БД роняет сервис. 18.10 версия
«maturity_terms_v1» не прошла CHECK (prompt_version ~ '^[a-z]+_v[0-9]+$'), и insight не запустился. Тест ловит это
до сборки образа.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from insight.application.prompt_builder import PromptBuilder

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"


def db_rules() -> tuple[re.Pattern[str], set[str]]:
    sql = "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql")))
    version = re.search(r"prompt_version\s+text PRIMARY KEY CHECK \(prompt_version ~ '([^']+)'\)", sql)
    purposes = re.findall(r"prompt_versions_purpose_check\s+CHECK \(purpose IN \(([^)]*)\)\)", sql)
    allowed = {item.strip().strip("'") for item in purposes[-1].split(",")} if purposes else set()
    return re.compile(version.group(1)), allowed


class PromptRegistryDbRulesTest(unittest.TestCase):
    def test_every_registered_prompt_fits_the_table_constraints(self) -> None:
        pattern, purposes = db_rules()
        self.assertTrue(purposes, "в миграциях не найдено ограничение назначений промптов")
        for definition in PromptBuilder(ROOT / "prompts", ROOT / "schemas").definitions():
            self.assertRegex(definition.prompt_version, pattern, definition.prompt_version)
            self.assertIn(definition.purpose.value, purposes, definition.prompt_version)

    def test_old_name_would_have_failed(self) -> None:
        pattern, _ = db_rules()
        self.assertIsNone(pattern.match("maturity_terms_v1"))
        self.assertIsNotNone(pattern.match("maturity_v1"))


if __name__ == "__main__":
    unittest.main()
