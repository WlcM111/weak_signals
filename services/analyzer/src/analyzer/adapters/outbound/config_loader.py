"""Загрузка конфигурации сервиса из файлов: реестр признаков, лексиконы, правила стадий.

Домен остаётся без файлового ввода-вывода: разбор выполняется здесь, на границе адаптеров.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from analyzer.domain.feature_registry import FeatureRegistry
from analyzer.domain.features import Lexicons, TermSet

LEXICON_FILES = {
    "emergence": ("emergence_ru.txt", "emergence_en.txt"),
    "maturity": ("maturity_ru.txt", "maturity_en.txt"),
    "hype": ("hype_ru.txt", "hype_en.txt"),
    "bigtech": ("bigtech.txt",),
    "stopwords": ("stopwords_ru.txt", "stopwords_en.txt"),
}
MAX_STAGE = 5


class ConfigError(RuntimeError):
    """Файл конфигурации отсутствует или имеет неверный формат."""


def load_feature_registry(path: Path) -> FeatureRegistry:
    """Читает нормативный реестр признаков `feature_registry_v1.json`."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"не удалось прочитать реестр признаков {path}: {error}") from error
    return FeatureRegistry.from_mapping(payload)


def load_lexicons(lexicon_dir: Path, stage_rules_path: Path) -> Lexicons:
    """Собирает лексиконы и правила стадий; отсутствие обязательного файла — отказ запуска."""
    directory = Path(lexicon_dir)
    terms = {
        group: _read_terms(directory, files) for group, files in LEXICON_FILES.items()
    }
    return Lexicons(
        emergence=TermSet.from_terms(terms["emergence"]),
        maturity=TermSet.from_terms(terms["maturity"]),
        hype=TermSet.from_terms(terms["hype"]),
        bigtech=TermSet.from_terms(terms["bigtech"]),
        stopwords=frozenset(word.lower() for word in terms["stopwords"]),
        stage_terms=load_stage_rules(stage_rules_path),
    )


def load_stage_rules(path: Path) -> dict[int, TermSet]:
    """Читает `stage_rules.yaml`: стадии 1..5 с маркерными терминами."""
    try:
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"не удалось прочитать правила стадий {path}: {error}") from error
    stages = (payload or {}).get("stages")
    if not isinstance(stages, dict) or not stages:
        raise ConfigError(f"{path}: ожидается раздел stages со стадиями 1..5")
    result: dict[int, TermSet] = {}
    for key, values in stages.items():
        stage = int(key)
        if not 1 <= stage <= MAX_STAGE:
            raise ConfigError(f"{path}: стадия {stage} вне диапазона 1..5")
        result[stage] = TermSet.from_terms(values or ())
    return result


def _read_terms(directory: Path, files: tuple[str, ...]) -> list[str]:
    """Читает список терминов: по одному в строке, `#` — комментарий."""
    terms: list[str] = []
    for name in files:
        path = directory / name
        if not path.is_file():
            raise ConfigError(f"лексикон не найден: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            cleaned = line.split("#", 1)[0].strip()
            if cleaned:
                terms.append(cleaned)
    return terms
