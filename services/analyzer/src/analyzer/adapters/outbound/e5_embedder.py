"""Локальная модель эмбеддингов intfloat/multilingual-e5-base (sentence-transformers).

Префикс e5 (`passage: ` / `query: `) добавляет адаптер: вызывающий код передаёт чистые тексты.
Векторы L2-нормализованы, поэтому косинусная близость считается скалярным произведением.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ws_common.logging import get_logger

DEFAULT_MODEL = "intfloat/multilingual-e5-base"


class E5Embedder:
    """Реализация порта `Embedder` на sentence-transformers (CPU)."""

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        batch_size: int = 32,
        torch_threads: int = 4,
        cache_dir: str | None = None,
    ) -> None:
        import torch  # noqa: PLC0415 - тяжёлая зависимость загружается только в рабочем контуре
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        torch.set_num_threads(max(1, torch_threads))
        self._model_name = model_name
        self._batch_size = batch_size
        self._log = get_logger("analyzer.embedder")
        self._model = SentenceTransformer(model_name, device="cpu", cache_folder=cache_dir)
        # sentence-transformers 5 переименовал метод; старое имя оставлено для версий 3–4
        dimension = getattr(self._model, "get_embedding_dimension", None)
        self._dims = int((dimension or self._model.get_sentence_embedding_dimension)())
        self._log.info("embedder.loaded", model=model_name, dims=self._dims, threads=torch_threads)

    @property
    def model_name(self) -> str:
        """Идентификатор модели эмбеддингов."""
        return self._model_name

    @property
    def dims(self) -> int:
        """Размерность вектора."""
        return self._dims

    def encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        """Векторы текстов с префиксом e5, L2-нормализованные."""
        if not texts:
            return np.zeros((0, self._dims), dtype=np.float32)
        prepared = [f"{prefix}{text}" for text in texts]
        vectors = self._model.encode(
            prepared,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)
