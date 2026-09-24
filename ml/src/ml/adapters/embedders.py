"""Эмбеддеры обучения.

`E5Embedder` — та же реализация, что и в analyzer (импортируется, а не копируется): совпадение
векторов обучения и инференса обязательно. `HashingEmbedder` — детерминированная замена без
загрузки моделей: нужна для тестов и офлайн-прогонов конвейера, идентификатор модели у неё другой,
поэтому analyzer такой артефакт не примет.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

import numpy as np

HASHING_DIMS = 256
HASHING_MODEL_NAME = "hashing-char-ngram-256"
_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


def build_embedder(kind: str, model_name: str, cache_dir: str | None = None, batch_size: int = 32):  # noqa: ANN201
    """Создаёт эмбеддер по имени: `e5` (рабочий контур) или `hashing` (офлайн и тесты)."""
    if kind == "hashing":
        return HashingEmbedder()
    from analyzer.adapters.outbound.e5_embedder import E5Embedder  # noqa: PLC0415 - тяжёлая зависимость

    return E5Embedder(model_name=model_name, batch_size=batch_size, cache_dir=cache_dir)


class HashingEmbedder:
    """Хеширующий эмбеддер: словные и символьные n-граммы → фиксированный вектор.

    Векторы детерминированы (одинаковы между запусками и машинами), близкие тексты получают
    близкие векторы за счёт общих n-грамм. Это не замена e5 по качеству — только по контракту.
    """

    def __init__(self, dims: int = HASHING_DIMS) -> None:
        self._dims = dims

    @property
    def model_name(self) -> str:
        """Идентификатор, попадающий в манифест модели."""
        return f"hashing-char-ngram-{self._dims}"

    @property
    def dims(self) -> int:
        """Размерность вектора."""
        return self._dims

    def encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        """Векторы текстов; префикс сохраняется в контракте, но на значение не влияет."""
        vectors = np.zeros((len(texts), self._dims), dtype=np.float32)
        for row, text in enumerate(texts):
            lowered = text.lower()
            for token in _TOKEN_RE.findall(lowered):
                self._add(vectors[row], f"w:{token}", 1.0)
                padded = f" {token} "
                for size in (3, 4):
                    for start in range(len(padded) - size + 1):
                        self._add(vectors[row], f"c:{padded[start : start + size]}", 0.5)
            norm = float(np.linalg.norm(vectors[row]))
            if norm > 0.0:
                vectors[row] /= norm
        return vectors

    def _add(self, vector: np.ndarray, key: str, weight: float) -> None:
        """Добавляет вклад признака в позицию, определяемую хешем (знак — из того же хеша)."""
        digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % self._dims
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign * weight
