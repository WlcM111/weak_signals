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

TOKEN_RE = re.compile(r"[a-zа-яё]+", re.IGNORECASE)
STEM = 6
# Поверхностные признаки, на которых модель v4 решала в живом конвейере 27.09 («2026» +0,54, «для» +0,17,
# «препринт» +0,19, «систем» −0,25): годы и числа не берутся вовсе, служебные слова и слова о типе источника — по списку.
STOP = frozenset({
    "для", "как", "нет", "или", "это", "что", "при", "его", "она", "они", "так", "также", "может", "быть", "был",
    "была", "были", "этот", "эта", "эти", "которы", "где", "когда", "чем", "более", "менее", "очень", "все", "всех",
    "уже", "ещё", "еще", "только", "над", "под", "без", "про", "через", "между", "после", "перед", "then", "the",
    "and", "for", "with", "from", "that", "this", "are", "was", "препри", "preprint", "статья", "статьи", "публик",
    "источн", "докуме", "систем", "технол", "решени", "примен", "исполь", "област", "направ", "метод", "подход",
})


NEGATORS = frozenset({"не", "без", "ни", "no", "not", "without", "non"})
CLAUSE_RE = re.compile(r"[.,;:!?()\[\]«»\"—–\n]+")


def _stems(text: str) -> list[str]:
    """Основы слов по фразам с учётом отрицания: «нет массового внедрения» и «массового внедрения нет» дают
    признаки «не_массов», а не «массов» (аудит 29.09: иначе отрицание терялось и обе фразы давали одно и то же)."""
    out: list[str] = []
    for clause in CLAUSE_RE.split((text or "").lower()):
        words = TOKEN_RE.findall(clause)
        stems: list[str] = []
        negate_next = False
        for index, word in enumerate(words):
            if word in NEGATORS or (word == "нет" and index < len(words) - 1):
                negate_next = True
                continue
            if word == "нет":  # «нет» в конце фразы отрицает всю фразу до него
                stems = [s if s.startswith("не_") else f"не_{s}" for s in stems]
                continue
            if len(word) < 3:
                continue
            stem = word[:STEM]
            if stem in STOP:
                continue
            stems.append(f"не_{stem}" if negate_next else stem)
            negate_next = False
        out.extend(stems)
        out.append("|")  # граница фразы: пары основ через неё не строятся
    return out


def features(text: str) -> dict[str, float]:
    """Бинарные признаки основ и пар основ внутри фразы (с отрицанием), нормированные на корень из их числа."""
    stems = _stems(text)
    words = [s for s in stems if s != "|"]
    keys = set(words) | {f"{a}_{b}" for a, b in zip(stems, stems[1:]) if a != "|" and b != "|"}
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
