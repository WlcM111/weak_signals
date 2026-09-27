"""Локальный классификатор слабого сигнала по профилю технологии (этап 1 ТЗ: обучение на датасете).

Вход — текст профиля: название и описание технологии с фактами о стадии и рынке. На датасете это строки,
в живом конвейере — профиль, который LLM готовит по найденным источникам. Признаки — основы слов (первые
6 букв) и пары соседних основ, бинарные и нормированные на длину текста (L2), чтобы длина описания не
решала класс. Модель — логистическая регрессия; веса лежат в JSON, предсказание — сумма весов и сигмоида.
Вклад признака — вес × значение: это ключевые предикторы в карточке (интерпретируемость по ТЗ).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
STEM = 6


def features(text: str) -> dict[str, float]:
    """Бинарные признаки основ и пар основ, нормированные на корень из их числа."""
    words = [word[:STEM] for word in TOKEN_RE.findall((text or "").lower()) if len(word) >= 3]
    keys = set(words) | {f"{a}_{b}" for a, b in zip(words, words[1:])}
    if not keys:
        return {}
    value = 1.0 / math.sqrt(len(keys))
    return {key: value for key in keys}


def logit(model: dict, text: str) -> float:
    weights = model["weights"]
    return model["intercept"] + sum(weights.get(key, 0.0) * value for key, value in features(text).items())


def predict(model: dict, text: str) -> float:
    """Вероятность слабого сигнала."""
    return 1.0 / (1.0 + math.exp(-max(min(logit(model, text), 30.0), -30.0)))


def contributions(model: dict, text: str, top: int = 8) -> list[tuple[str, float]]:
    """Самые весомые признаки текста: (признак, вклад в логит), по убыванию модуля."""
    weights = model["weights"]
    rows = [(key, weights[key] * value) for key, value in features(text).items() if key in weights]
    return sorted(rows, key=lambda row: -abs(row[1]))[:top]


def load(path: str | Path) -> dict | None:
    """Модель из JSON; None, если файла нет или структура не та."""
    try:
        model = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return model if isinstance(model.get("weights"), dict) and "intercept" in model else None
