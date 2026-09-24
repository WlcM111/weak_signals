"""Ключевые фразы кластера: n-граммы заголовков, ранжирование по центроиду и MMR (§12.5, шаг 6)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

import numpy as np

MIN_NGRAM = 1
MAX_NGRAM = 3
MAX_PHRASE_LENGTH = 200
MMR_LAMBDA = 0.7
MAX_CANDIDATE_PHRASES = 60
_WORD_RE = re.compile(r"[\w][\w+-]*", re.UNICODE)


def candidate_phrases(titles: Sequence[str], stopwords: frozenset[str]) -> list[str]:
    """N-граммы 1..3 из заголовков без стоп-слов по краям, упорядоченные по частоте."""
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for title in titles:
        words = _WORD_RE.findall(title)
        for size in range(MIN_NGRAM, MAX_NGRAM + 1):
            for start in range(len(words) - size + 1):
                phrase_words = words[start : start + size]
                key = " ".join(word.lower() for word in phrase_words)
                if not _is_acceptable(phrase_words, key, stopwords):
                    continue
                counter[key] += 1
                display.setdefault(key, " ".join(phrase_words))
    ordered = sorted(counter.items(), key=lambda item: (-item[1], -len(item[0].split()), item[0]))
    return [display[key] for key, _ in ordered[:MAX_CANDIDATE_PHRASES]]


def _is_acceptable(words: Sequence[str], key: str, stopwords: frozenset[str]) -> bool:
    """Фраза годится, если не начинается и не кончается стоп-словом и содержит значащее слово."""
    if len(key) > MAX_PHRASE_LENGTH or len(key) < 3:
        return False
    lowered = [word.lower() for word in words]
    if lowered[0] in stopwords or lowered[-1] in stopwords:
        return False
    if all(word in stopwords for word in lowered):
        return False
    return any(len(word) > 2 and not word.isdigit() for word in lowered)


def select_by_mmr(
    phrases: Sequence[str],
    phrase_vectors: np.ndarray,
    cluster_centroid: np.ndarray,
    top_k: int,
    lambda_: float = MMR_LAMBDA,
) -> list[str]:
    """Maximal Marginal Relevance: близость к центроиду против разнообразия внутри выборки."""
    if not phrases or phrase_vectors.size == 0:
        return []
    vectors = np.asarray(phrase_vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    vectors = vectors / norms
    target = np.asarray(cluster_centroid, dtype=np.float32)
    target_norm = float(np.linalg.norm(target))
    relevance = vectors @ target / target_norm if target_norm > 0 else np.zeros(len(phrases))
    selected: list[int] = []
    remaining = list(range(len(phrases)))
    while remaining and len(selected) < top_k:
        if not selected:
            best = max(remaining, key=lambda index: float(relevance[index]))
        else:
            similarity = vectors[remaining] @ vectors[selected].T
            penalties = similarity.max(axis=1)
            scores = lambda_ * relevance[remaining] - (1.0 - lambda_) * penalties
            best = remaining[int(np.argmax(scores))]
        selected.append(best)
        remaining.remove(best)
    return [phrases[index] for index in selected]


def fallback_title(titles: Sequence[str]) -> str:
    """Метка кластера, когда ключевых фраз выделить не удалось: усечённый первый заголовок."""
    for title in titles:
        cleaned = " ".join(title.split())
        if cleaned:
            return cleaned[:MAX_PHRASE_LENGTH]
    return "Без названия"
