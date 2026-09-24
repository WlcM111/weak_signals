"""Матрицы признаков v2 для строк A и B тем же `observation_features`, что в analyzer."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from analyzer.domain.features import Lexicons
from analyzer.domain.features_v2 import (
    EMBEDDING_COMPONENTS,
    FEATURE_NAMES_V2,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    ObservationVectors,
    Projection,
    observation_features,
)

from ml.seq.data import Obs


def _key(prefix: str, text: str) -> str:
    return hashlib.sha256(f"{prefix}|{text}".encode()).hexdigest()


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


class Featurizer:
    """Эмбеддинги с дисковым кешем и признаки v2."""

    def __init__(self, embedder, lexicons: Lexicons, glossary: dict[str, str], cache_dir: Path | None = None) -> None:  # noqa: ANN001
        self._embedder = embedder
        self._lexicons = lexicons
        self.glossary = dict(glossary)
        self._cache: dict[str, np.ndarray] = {}
        safe = "".join(ch if ch.isalnum() else "_" for ch in embedder.model_name)
        self._cache_path = (Path(cache_dir) / f"emb-{safe}.npz") if cache_dir else None
        if self._cache_path and self._cache_path.is_file():
            with np.load(self._cache_path, allow_pickle=False) as payload:
                for key, vector in zip(payload["keys"], payload["vectors"], strict=True):
                    self._cache[str(key)] = np.asarray(vector, dtype=np.float64)
        self.cluster_query_impute: float | None = None

    @property
    def embedding_model(self) -> str:
        """Идентификатор эмбеддера (попадает в контрольные точки и артефакт)."""
        return str(self._embedder.model_name)

    def vectors(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        """Векторы текстов с кешем по (префикс, текст)."""
        keys = [_key(prefix, text) for text in texts]
        missing = list(dict.fromkeys(text for text, key in zip(texts, keys, strict=True) if key not in self._cache))
        if missing:
            encoded = np.asarray(self._embedder.encode(missing, prefix), dtype=np.float64)
            for text, vector in zip(missing, encoded, strict=True):
                self._cache[_key(prefix, text)] = vector
        if not keys:
            return np.zeros((0, int(self._embedder.dims)))
        return np.vstack([self._cache[key] for key in keys])

    def save_cache(self) -> None:
        """Сохраняет кеш эмбеддингов (безопасное продолжение долгих прогонов)."""
        if not self._cache_path:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        keys = sorted(self._cache)
        np.savez_compressed(self._cache_path, keys=np.array(keys), vectors=np.vstack([self._cache[k] for k in keys]))

    def fit_projection(self, observations: Sequence[Obs], components: int = EMBEDDING_COMPONENTS) -> Projection:
        """PCA названий A-train: центр, компоненты (знак фиксирован), масштаб; замораживается."""
        matrix = np.vstack([_unit(v) for v in self.vectors([obs.title for obs in observations], PASSAGE_PREFIX)])
        mean = matrix.mean(axis=0)
        _, _, vt = np.linalg.svd(matrix - mean, full_matrices=False)
        comps = vt[:components].copy()
        signs = np.sign(comps[np.arange(comps.shape[0]), np.abs(comps).argmax(axis=1)])
        comps *= np.where(signs == 0, 1.0, signs)[:, None]
        scale = np.maximum((matrix - mean) @ comps.T, -1e9).std(axis=0, ddof=1)
        return Projection(mean=mean, components=comps, scale=np.maximum(scale, 1e-6))

    def matrix(self, observations: Sequence[Obs], projection: Projection) -> np.ndarray:
        """Матрица n × len(FEATURE_NAMES_V2)."""
        topics = sorted({obs.topic for obs in observations if obs.topic})
        topic_vectors = dict(zip(topics, self.vectors(topics, QUERY_PREFIX), strict=True)) if topics else {}
        texts: list[str] = []
        starts: list[int] = []
        for obs in observations:
            starts.append(len(texts))
            texts.extend([obs.title, *[item.title for item in obs.evidence]])
        vectors = self.vectors(texts, PASSAGE_PREFIX)
        rows: list[list[float]] = []
        for obs, start in zip(observations, starts, strict=True):
            count = len(obs.evidence)
            title_vec = vectors[start]
            evidence = vectors[start + 1 : start + 1 + count] if count else np.zeros((0, title_vec.shape[0]))
            query = topic_vectors.get(obs.topic) if obs.topic else None
            similarity = obs.observed_query_sim if obs.observed_query_sim is not None else self.cluster_query_impute
            values = observation_features(
                obs.topic, obs.title, obs.evidence, ObservationVectors(query, title_vec, evidence), projection,
                self._lexicons, obs.as_of_year, self.glossary,
                cluster_query_similarity=similarity if obs.topic else None,
            )
            rows.append([values[name] for name in FEATURE_NAMES_V2])
        return np.asarray(rows, dtype=np.float64)
