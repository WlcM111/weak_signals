"""Запись входов анализа для повторяемой оценки без сети (аудит A3, режим А; этап 2).

Анализ зависит от внешних и меняющихся входов: эмбеддингов e5 (их считает модель, и векторы из кеша и свежие могут
отличаться в последних разрядах), ответов Wikipedia, текущей даты (признаки свежести) и настроек. Запись сохраняет
ровно те значения, которые видел конвейер: векторы документов по `document_id`, остальные вызовы эмбеддера — в
порядке вызова, ответы энциклопедии — по вызовам, дату признаков и настройки. Там же — воронка документов:
где каждый документ выбыл (почти-дубль, порог близости, одиночный кластер, отсечение по числу кандидатов).
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from analyzer.application.dto import EncyclopediaHit


@dataclass(slots=True)
class TextCall:
    """Один вызов эмбеддера вне векторов документов: префикс, тексты и полученные векторы."""

    prefix: str
    texts: tuple[str, ...]
    vectors: np.ndarray


@dataclass(slots=True)
class AnalysisCapture:
    """Входы одного анализа: без них анализ нельзя повторить без сети и с тем же результатом."""

    query_text: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    model_version_id: str = ""
    embedding_model: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    as_of: datetime | None = None
    funnel: dict[str, Any] = field(default_factory=dict)
    encyclopedia: list[dict[str, Any]] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    document_keys: list[str] = field(default_factory=list)
    document_vectors: np.ndarray | None = None
    text_calls: list[TextCall] = field(default_factory=list)

    def record_call(self, prefix: str, texts: Sequence[str], vectors: np.ndarray) -> None:
        """Запоминает вызов эмбеддера с векторами в исходном типе (без потери разрядов)."""
        self.text_calls.append(TextCall(prefix, tuple(texts), np.array(vectors, copy=True)))

    def record_encyclopedia(
        self, language: str, titles: Sequence[str], hits: Sequence[EncyclopediaHit] | None, error: str | None
    ) -> None:
        """Запоминает ответ энциклопедии на пачку названий или отказ пачки."""
        entry: dict[str, Any] = {"language": language, "titles": list(titles)}
        if error is not None:
            entry["error"] = error
        else:
            entry["hits"] = [_hit_to_dict(hit) for hit in hits or ()]
        self.encyclopedia.append(entry)


class CapturingEmbedder:
    """Эмбеддер-обёртка: отдаёт векторы исходного и записывает вызов в запись анализа текущего потока."""

    def __init__(self, embedder: Any, local: threading.local) -> None:
        self._embedder = embedder
        self._local = local

    @property
    def model_name(self) -> str:
        """Имя модели исходного эмбеддера."""
        return str(self._embedder.model_name)

    @property
    def dims(self) -> int:
        """Размерность векторов исходного эмбеддера."""
        return int(self._embedder.dims)

    def encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        """Векторы исходного эмбеддера; при активной записи вызов попадает в неё."""
        vectors = self._embedder.encode(texts, prefix)
        capture = getattr(self._local, "capture", None)
        if capture is not None:
            capture.record_call(prefix, list(texts), np.asarray(vectors))
        return vectors


def config_snapshot(config: Any) -> dict[str, Any]:
    """Настройки конвейера в виде JSON (кортежи — списками)."""
    return json.loads(json.dumps(dataclasses.asdict(config), ensure_ascii=False, default=str))


def pack_vectors(capture: AnalysisCapture) -> bytes:
    """Векторы записи в один архив npz (без pickle); строки — массивами Unicode."""
    texts = [text for call in capture.text_calls for text in call.texts]
    by_type: dict[str, list[np.ndarray]] = {"float32": [], "float64": []}
    dtypes: list[str] = []
    for call in capture.text_calls:
        dtype = "float64" if call.vectors.dtype == np.float64 else "float32"
        dtypes.append(dtype)
        by_type[dtype].append(np.asarray(call.vectors, dtype=dtype).reshape(len(call.texts), -1))
    documents = capture.document_vectors if capture.document_vectors is not None else np.zeros((0, 0), np.float32)
    buffer = io.BytesIO()
    np.savez_compressed(
        buffer,
        doc_ids=np.array(capture.document_ids, dtype=str),
        doc_keys=np.array(capture.document_keys, dtype=str),
        doc_vectors=np.asarray(documents, dtype=np.float32),
        call_prefixes=np.array([call.prefix for call in capture.text_calls], dtype=str),
        call_dtypes=np.array(dtypes, dtype=str),
        call_counts=np.array([len(call.texts) for call in capture.text_calls], dtype=np.int64),
        call_texts=np.array(texts, dtype=str),
        vectors_float32=_stack(by_type["float32"], np.float32),
        vectors_float64=_stack(by_type["float64"], np.float64),
    )
    return buffer.getvalue()


def unpack_vectors(blob: bytes) -> tuple[dict[str, tuple[str, np.ndarray]], list[TextCall]]:
    """Обратное к `pack_vectors`: векторы документов по id и вызовы эмбеддера в исходном порядке."""
    with np.load(io.BytesIO(blob), allow_pickle=False) as data:
        doc_ids = [str(value) for value in data["doc_ids"]]
        doc_keys = [str(value) for value in data["doc_keys"]]
        doc_vectors = np.asarray(data["doc_vectors"], dtype=np.float32)
        prefixes = [str(value) for value in data["call_prefixes"]]
        dtypes = [str(value) for value in data["call_dtypes"]]
        counts = [int(value) for value in data["call_counts"]]
        texts = [str(value) for value in data["call_texts"]]
        pools = {"float32": np.asarray(data["vectors_float32"]), "float64": np.asarray(data["vectors_float64"])}
    documents = {doc_id: (key, doc_vectors[row]) for row, (doc_id, key) in enumerate(zip(doc_ids, doc_keys, strict=True))}
    calls: list[TextCall] = []
    offsets = {"float32": 0, "float64": 0}
    text_offset = 0
    for prefix, dtype, count in zip(prefixes, dtypes, counts, strict=True):
        start = offsets[dtype]
        calls.append(TextCall(prefix, tuple(texts[text_offset : text_offset + count]), pools[dtype][start : start + count]))
        offsets[dtype] += count
        text_offset += count
    return documents, calls


def vectors_digest(blob: bytes) -> str:
    """SHA-256 архива векторов (контроль целостности при выгрузке)."""
    return hashlib.sha256(blob).hexdigest()


def hit_from_dict(payload: dict[str, Any]) -> EncyclopediaHit:
    """Ответ энциклопедии из записи."""
    created = payload.get("created_at")
    return EncyclopediaHit(
        title=str(payload["title"]),
        exists=bool(payload["exists"]),
        page_url=str(payload.get("page_url") or ""),
        pageviews_30d=int(payload.get("pageviews_30d", -1)),
        created_at=datetime.fromisoformat(created) if created else None,
    )


def _hit_to_dict(hit: EncyclopediaHit) -> dict[str, Any]:
    return {"title": hit.title, "exists": hit.exists, "page_url": hit.page_url, "pageviews_30d": hit.pageviews_30d,
            "created_at": hit.created_at.isoformat() if hit.created_at else None}


def _stack(parts: list[np.ndarray], dtype: type) -> np.ndarray:
    if not parts:
        return np.zeros((0, 0), dtype=dtype)
    return np.vstack(parts).astype(dtype, copy=False)
