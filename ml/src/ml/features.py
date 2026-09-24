"""Признаки обучения — тот же код, что использует analyzer при инференсе (§6.3 HANDOFF).

Единственный источник истины: функции `analyzer.domain.features`. Модуль не копирует формулы,
а только раскладывает данные обучающей строки по группам признаков реестра и собирает матрицу.
Эквивалентность с инференсом проверяется тестом `ml/tests/test_features_parity.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from analyzer.adapters.outbound.config_loader import load_feature_registry, load_lexicons
from analyzer.domain.entities import DocumentRef
from analyzer.domain.feature_registry import FeatureRegistry
from analyzer.domain.features import (
    EncyclopediaSignal,
    Lexicons,
    collection_features,
    embedding_features,
    empty_collection_features,
    encyclopedia_features,
    lexical_features,
)
from analyzer.domain.values import SourceType, TrustLevel

from ml.dataset import TrainingRow

PASSAGE_PREFIX = "passage: "
SELF_QUERY_SIMILARITY = 1.0


@dataclass(frozen=True, slots=True)
class FeatureContext:
    """Всё, что нужно для вычисления признаков: реестр, лексиконы и момент расчёта."""

    registry: FeatureRegistry
    lexicons: Lexicons
    now: datetime

    @staticmethod
    def load(registry_path: Path, lexicon_dir: Path, stage_rules_path: Path, now: datetime) -> FeatureContext:
        """Загружает реестр и лексиконы теми же загрузчиками, что и сервис."""
        return FeatureContext(
            registry=load_feature_registry(registry_path),
            lexicons=load_lexicons(lexicon_dir, stage_rules_path),
            now=now,
        )


def documents_from_enrichment(payload: dict[str, Any]) -> list[DocumentRef]:
    """Восстанавливает документы ENRICHMENT-коллекции из сохранённого JSON."""
    documents: list[DocumentRef] = []
    for item in payload.get("documents", []):
        published = item.get("published_at")
        documents.append(
            DocumentRef(
                document_id=str(item.get("document_id", "")),
                title=item.get("title", ""),
                text=item.get("text", ""),
                language_code=item.get("language_code", "en"),
                source_type=SourceType(item.get("source_type", "OTHER")),
                trust_level=TrustLevel(item.get("trust_level", "LOW")),
                origin_domain=item.get("origin_domain", ""),
                url=item.get("url", ""),
                published_at=datetime.fromisoformat(published) if published else None,
                citation_count=item.get("citation_count"),
                engagement_count=item.get("engagement_count"),
            )
        )
    return documents


def encyclopedia_from_enrichment(payload: dict[str, Any]) -> EncyclopediaSignal:
    """Восстанавливает лучший результат Wikipedia из сохранённого JSON."""
    hit = payload.get("encyclopedia") or {}
    if not hit.get("exists"):
        return EncyclopediaSignal()
    created = hit.get("created_at")
    return EncyclopediaSignal(
        exists=True,
        pageviews_30d=max(int(hit.get("pageviews_30d", 0) or 0), 0),
        created_at=datetime.fromisoformat(created) if created else None,
    )


def row_features(
    row: TrainingRow,
    context: FeatureContext,
    vector: np.ndarray,
    weak_centroid: np.ndarray | None,
    mature_centroid: np.ndarray | None,
    enrichment: dict[str, Any] | None = None,
) -> dict[str, float]:
    """25 признаков реестра для одной обучающей строки.

    `emb_sim_query` при обучении равен 1.0 (§12.7 ТЗ): запроса пользователя нет, а признак
    участвует только в правиле OFF_TOPIC и в модель не подаётся.
    """
    documents = documents_from_enrichment(enrichment) if enrichment else []
    collection = collection_features(documents, context.now) if documents else empty_collection_features()
    encyclopedia = (
        encyclopedia_features(encyclopedia_from_enrichment(enrichment), context.now)
        if enrichment
        else encyclopedia_features(EncyclopediaSignal(), context.now)
    )
    values = {
        **lexical_features(row.text, context.lexicons, context.now),
        **collection,
        **encyclopedia,
        **embedding_features(vector, weak_centroid, mature_centroid, None),
        "emb_sim_query": SELF_QUERY_SIMILARITY,
    }
    return values


def build_matrix(
    rows: Sequence[dict[str, float]], registry: FeatureRegistry, model_only: bool = True
) -> np.ndarray:
    """Матрица признаков в порядке реестра с клиппингом по диапазонам."""
    if not rows:
        width = len(registry.model_names if model_only else registry.names)
        return np.zeros((0, width), dtype=np.float64)
    build = registry.model_vector if model_only else registry.vector
    return np.asarray([build(values) for values in rows], dtype=np.float64)


def centroids_from(vectors: np.ndarray, labels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Эталонные центроиды: среднее эмбеддингов позитивов и негативов обучающей части.

    Считается только по переданной части выборки — вызывающий код обязан передавать train-фолд,
    иначе признаки `emb_sim_*` утекут (§6.1 HANDOFF).
    """
    weak = _unit_mean(vectors[labels == 1])
    mature = _unit_mean(vectors[labels == 0])
    return weak, mature


def _unit_mean(vectors: np.ndarray) -> np.ndarray:
    """Нормализованное среднее набора векторов."""
    if vectors.shape[0] == 0:
        return np.zeros(vectors.shape[1], dtype=np.float32)
    mean = vectors.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    return (mean if norm == 0.0 else mean / norm).astype(np.float32)


def empty_collection_defaults(context: FeatureContext, rows: Sequence[dict[str, float]]) -> dict[str, float]:
    """Значения-заглушки коллекционных и энциклопедических признаков для ScoreText без обогащения.

    Доли и счётчики — «пустая коллекция» (0), возрастные признаки — медиана обучающей выборки
    (§12.7 ТЗ). Записываются в `rules.json` и `feature_defaults.json` артефакта.
    """
    defaults = dict(empty_collection_features())
    defaults.update(dict.fromkeys(("wiki_exists", "wiki_pageviews_30d_log", "wiki_age_years"), 0.0))
    for name in ("recency_median_days", "first_seen_years_ago", "citation_median_log"):
        observed = [values[name] for values in rows if values.get(name, 0.0) > 0.0]
        if observed:
            defaults[name] = float(np.median(observed))
    return defaults
