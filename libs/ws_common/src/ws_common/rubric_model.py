"""Локальная вероятностная модель слабого сигнала: признаки рубрики LLM, состава источников и analyzer.

Один модуль и для обучения (ml.rubric_ranker), и для работы сервиса (orchestrator): признаки считаются
одинаково. Модель — логистическая регрессия со стандартизацией, хранится в JSON (коэффициенты, средние,
масштабы); предсказание — скалярное произведение и сигмоида, без внешних библиотек.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from pathlib import Path

CODES = ("R", "U", "N-GEN", "N-OVR", "N-OFF", "N-MAT", "N-NOI", "N-HYP", "N-FUND")
NON_INDEPENDENT = frozenset({"CODE_REPOSITORY", "PRESS_RELEASE", "CORPORATE_BLOG", "SOCIAL_MEDIA", "OTHER",
                             "VACANCY", "ENCYCLOPEDIA"})
MARKET = frozenset({"NEWS", "INDUSTRY_MEDIA", "PRESS_RELEASE", "ANALYTICAL_REPORT"})
REVIEW_RE = re.compile(
    r"\b(review|survey|overview|roadmap|state[- ]of[- ]the[- ]art|perspective|tutorial|progress|advances|trends|"
    r"prospects|challenges|обзор|перспектив|тенденци)\w*", re.IGNORECASE)
FEATURES = ("code_R", "code_U", "code_GEN", "code_OVR", "code_OFF", "code_MAT", "code_NOISE", "llm_confidence",
            "stage", "trend", "sources", "independent_share", "market_share", "patent_share", "code_share",
            "review_share", "recent_share", "analyzer_score")


def features(code: str, confidence: float, stage: int, trend: int, source_types: Sequence[str],
             trust_levels: Sequence[str], titles: Sequence[str], years: Sequence[int | None],
             analyzer_score: float | None) -> dict[str, float]:
    """Признаки кандидата; доли считаются по его источникам, пропущенный скор analyzer — 0,5."""
    total = max(len(source_types), 1)
    independent = sum(1 for kind, trust in zip(source_types, trust_levels)
                      if kind not in NON_INDEPENDENT and not (kind == "PREPRINT" and trust != "HIGH"))
    dated = [year for year in years if year]
    recent = (sum(1 for year in dated if year >= max(dated) - 1) / len(dated)) if dated else 0.0
    return {
        "code_R": float(code == "R"), "code_U": float(code == "U"), "code_GEN": float(code == "N-GEN"),
        "code_OVR": float(code == "N-OVR"), "code_OFF": float(code == "N-OFF"), "code_MAT": float(code == "N-MAT"),
        "code_NOISE": float(code in ("N-NOI", "N-HYP", "N-FUND")), "llm_confidence": float(confidence),
        "stage": float(stage), "trend": float(trend), "sources": float(len(source_types)),
        "independent_share": independent / total,
        "market_share": sum(1 for kind in source_types if kind in MARKET) / total,
        "patent_share": sum(1 for kind in source_types if kind == "PATENT") / total,
        "code_share": sum(1 for kind in source_types if kind == "CODE_REPOSITORY") / total,
        "review_share": sum(1 for title in titles if REVIEW_RE.search(title or "")) / total,
        "recent_share": recent, "analyzer_score": 0.5 if analyzer_score is None else float(analyzer_score),
    }


def predict(model: dict, values: dict[str, float]) -> float:
    """Вероятность слабого сигнала по модели из JSON."""
    z = model["intercept"]
    for name, coef, mean, scale in zip(model["features"], model["coef"], model["mean"], model["scale"]):
        z += coef * (values.get(name, mean) - mean) / (scale or 1.0)
    return 1.0 / (1.0 + math.exp(-max(min(z, 30.0), -30.0)))


def load(path: str | Path) -> dict | None:
    """Модель из JSON; None, если файла нет или он не той структуры."""
    try:
        model = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    keys = {"features", "coef", "mean", "scale", "intercept"}
    return model if keys <= set(model) and list(model["features"]) == list(FEATURES) else None


LABELS_RU = {
    "code_R": "Рубрика LLM: слабый сигнал", "code_U": "Рубрика LLM: данных недостаточно",
    "code_GEN": "Рубрика LLM: общее понятие", "code_OVR": "Рубрика LLM: обзор", "code_OFF": "Рубрика LLM: не по теме",
    "code_MAT": "Рубрика LLM: зрелая технология", "code_NOISE": "Рубрика LLM: шум или хайп",
    "llm_confidence": "Самооценка уверенности LLM", "stage": "Стадия (1 — исследование … 4 — раннее внедрение)",
    "trend": "Тренд (1 — слабый интерес … 3 — быстрый рост)", "sources": "Число источников",
    "independent_share": "Доля независимых доверенных источников", "market_share": "Доля рыночных источников (СМИ)",
    "patent_share": "Доля патентов", "code_share": "Доля репозиториев кода", "review_share": "Доля обзоров",
    "recent_share": "Доля публикаций двух последних лет", "analyzer_score": "Скор модели analyzer",
}


def contributions(model: dict, values: dict[str, float]) -> list[tuple[str, float, float]]:
    """Вклад каждого признака в логит (коэффициент × стандартизованное значение), по убыванию модуля."""
    rows = [(name, values.get(name, mean), coef * (values.get(name, mean) - mean) / (scale or 1.0))
            for name, coef, mean, scale in zip(model["features"], model["coef"], model["mean"], model["scale"])]
    return sorted(rows, key=lambda row: -abs(row[2]))
