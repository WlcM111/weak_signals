"""Клиент collector для обогащения обучающих строк.

Переиспользует синхронный клиент analyzer (`CollectorGrpcClient`): у обучения и инференса должен
быть один и тот же способ получать документы, иначе коллекционные признаки разойдутся.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ws_common.logging import get_logger

ENRICHMENT_CHUNK_SIZE = 200
ENRICHMENT_MAX_DOCUMENTS = 200
POLL_SECONDS = 3.0


class CollectorEnricher:
    """Запускает ENRICHMENT-коллекции и сохраняет результат в `data/enriched/<row_id>.json`."""

    def __init__(self, address: str, timeout_seconds: float = 90.0) -> None:
        from analyzer.adapters.outbound.collector_grpc import CollectorGrpcClient  # noqa: PLC0415
        from ws_common.grpc_clients import build_channel  # noqa: PLC0415

        self._channel = build_channel(address, caller="trainer")
        self._client = CollectorGrpcClient(self._channel)
        self._timeout = timeout_seconds
        self._log = get_logger("ml.enrich")

    def enrich(self, idempotency_key: str, title_ru: str, title_en: str) -> dict[str, Any]:
        """Собирает коллекцию по названию и возвращает документы и сведения Wikipedia."""
        import time  # noqa: PLC0415

        collection_id = self._client.start_enrichment(idempotency_key, title_ru)
        deadline = time.monotonic() + self._timeout
        while True:
            info = self._client.get_collection(collection_id)
            if info.is_terminal:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"коллекция {collection_id} не завершилась за {self._timeout} с")
            time.sleep(POLL_SECONDS)
        documents = list(
            self._client.stream_documents(collection_id, ENRICHMENT_CHUNK_SIZE, ENRICHMENT_MAX_DOCUMENTS)
        )
        encyclopedia = self._best_encyclopedia(title_ru, title_en)
        return {
            "collection_id": collection_id,
            "status": info.status,
            "collected_at": datetime.now(UTC).isoformat(),
            "documents": [_document_json(document) for document in documents],
            "encyclopedia": encyclopedia,
        }

    def _best_encyclopedia(self, title_ru: str, title_en: str) -> dict[str, Any]:
        """Лучший результат `CheckEncyclopedia` по русскому и английскому названию."""
        best: dict[str, Any] = {"exists": False}
        for language, title in (("ru", title_ru), ("en", title_en or title_ru)):
            for hit in self._client.check_encyclopedia([title], language):
                if hit.exists and max(hit.pageviews_30d, 0) >= int(best.get("pageviews_30d", 0) or 0):
                    best = {
                        "exists": True,
                        "title": hit.title,
                        "language": language,
                        "page_url": hit.page_url,
                        "pageviews_30d": max(hit.pageviews_30d, 0),
                        "created_at": hit.created_at.isoformat() if hit.created_at else None,
                    }
        return best

    def close(self) -> None:
        """Закрывает канал."""
        self._channel.close()


class FixtureEnricher:
    """Фикстурные коллекции из каталога: конвейер работает без сети и без collector."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._log = get_logger("ml.enrich")

    def enrich(self, idempotency_key: str, title_ru: str, title_en: str) -> dict[str, Any]:
        """Читает заранее записанный ответ; отсутствие файла — пустая коллекция."""
        row_id = idempotency_key.split(":")[1] if ":" in idempotency_key else idempotency_key
        path = self._directory / f"{row_id}.json"
        if not path.is_file():
            return {"collection_id": "", "status": "MISSING", "documents": [], "encyclopedia": {}}
        return json.loads(path.read_text(encoding="utf-8"))


def _document_json(document: Any) -> dict[str, Any]:
    """Документ в память конвейера → JSON (только поля, нужные для признаков)."""
    return {
        "document_id": document.document_id,
        "title": document.title,
        "text": document.text,
        "url": document.url,
        "language_code": document.language_code,
        "source_type": document.source_type.value,
        "trust_level": document.trust_level.value,
        "origin_domain": document.origin_domain,
        "published_at": document.published_at.isoformat() if document.published_at else None,
        "citation_count": document.citation_count,
        "engagement_count": document.engagement_count,
    }
