"""Запись промптов и ответов LLM для повторяемой оценки (аудит A3, режим А; этап 2).

Решение владельца проекта (03.10.2026): тексты промптов и ответов сохраняются — это осознанная замена правила §14.5
ТЗ «ни промпт, ни ответ не сохраняются» ради воспроизводимости оценки. Журнал `llm_calls` по-прежнему хранит
только метаданные; полные тексты пишутся в отдельную таблицу `llm_recordings` (WS_LLM_RECORD_ENABLED=false
выключает запись).

Ключ повтора — SHA-256 канонического JSON запроса: сообщения, схема ответа (её хеш), лимит токенов и температура.
Повтор режима А находит записанный ответ по этому ключу у того же провайдера и в том же порядке вызовов.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMRecording:
    """Один вызов провайдера LLM: запрос целиком, итог и ответ."""

    correlation_id: str
    purpose: str
    prompt_version: str
    request_sha256: str
    replay_key: str
    provider: str
    model: str
    status: str
    messages: tuple[tuple[str, str], ...]
    json_schema_sha256: str
    max_tokens: int
    temperature: float
    response_text: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    retry_after_seconds: float | None = None


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def schema_digest(json_schema: dict | None) -> str:
    """SHA-256 схемы ответа (пусто, если схемы нет)."""
    return "" if json_schema is None else hashlib.sha256(_canonical(json_schema)).hexdigest()


def replay_key(messages: Sequence[Any], json_schema: dict | None, max_tokens: int, temperature: float) -> str:
    """Ключ повтора: одинаковый для одинаковых запросов к модели, независимо от задания и времени."""
    pairs = [[str(getattr(m, "role", m[0] if isinstance(m, tuple | list) else "")),
              str(getattr(m, "content", m[1] if isinstance(m, tuple | list) else ""))] for m in messages]
    payload = {"messages": pairs, "schema": schema_digest(json_schema), "max_tokens": int(max_tokens),
               "temperature": round(float(temperature), 6)}
    return hashlib.sha256(_canonical(payload)).hexdigest()
