"""Этап 5.5: вероятность «зрелая технология» для готовых карточек — локальная модель.

Эмбеддинги текста карточки (bge-m3) и логистическая регрессия, обученная `python -m analyzer.train_maturity` на ручной
разметке трёх прогонов (369 карточек). Проверка с исключением прогона (19.10.2026, только карточки с научными
источниками, порог обучения 2 %): pf — убрано 39 % зрелых при потере 6 % слабых сигналов, e2e501 — 28 % и 6 %.

Файл модели (JSON в хранилище моделей): model, prefix, coef, intercept, threshold, version. Нет файла — модель не
загружена: ответ без оценок, и orchestrator ничего не отсеивает. Модель эмбеддингов загружается при первом вызове.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_TEXTS = 60
MAX_TEXT_CHARS = 2000


@dataclass(frozen=True, slots=True)
class MaturityModel:
    model_name: str
    prefix: str
    coef: tuple[float, ...]
    intercept: float
    threshold: float
    version: str

    @staticmethod
    def load(path: Path) -> MaturityModel | None:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return MaturityModel(str(data["model"]), str(data.get("prefix", "")), tuple(float(x) for x in data["coef"]),
                             float(data["intercept"]), float(data["threshold"]), str(data["version"]))


class ScoreMaturity:
    """Оценки зрелости для текстов карточек; без файла модели — пустой ответ."""

    def __init__(self, model_path: Path, hf_home: Path, batch_size: int = 16,
                 embedder_factory: Callable[[str], object] | None = None) -> None:
        self._path = Path(model_path)
        self._hf_home = Path(hf_home)
        self._batch = batch_size
        self._factory = embedder_factory
        self._model: MaturityModel | None = None
        self._loaded_mtime: float | None = None
        self._embedder = None

    def _current(self) -> MaturityModel | None:
        if not self._path.is_file():
            self._model, self._loaded_mtime = None, None
            return None
        mtime = self._path.stat().st_mtime
        if self._model is None or mtime != self._loaded_mtime:  # новый файл модели подхватывается без перезапуска
            self._model, self._loaded_mtime = MaturityModel.load(self._path), mtime
        return self._model

    def _encoder(self, model_name: str):  # noqa: ANN202
        if self._embedder is None:
            if self._factory is not None:
                self._embedder = self._factory(model_name)
            else:
                from analyzer.adapters.outbound.e5_embedder import E5Embedder  # noqa: PLC0415 - тяжёлый импорт
                self._embedder = E5Embedder(model_name, batch_size=self._batch, cache_dir=str(self._hf_home))
        return self._embedder

    def execute(self, texts: Sequence[str]) -> tuple[list[float], float, str]:
        model = self._current()
        if model is None or not texts:
            return [], 0.0, "" if model is None else model.version
        batch = [text[:MAX_TEXT_CHARS] for text in list(texts)[:MAX_TEXTS]]
        vectors = self._encoder(model.model_name).encode(batch, model.prefix)
        scores = []
        for vector in vectors:
            z = model.intercept + sum(float(a) * b for a, b in zip(vector, model.coef, strict=False))
            scores.append(1.0 / (1.0 + math.exp(-max(min(z, 50.0), -50.0))))
        return scores, model.threshold, model.version
