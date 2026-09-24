"""Группы связанных наблюдений: одна технология/событие в пределах темы (общие URL или название)."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

_NORM_RE = re.compile(r"[^0-9a-zа-яё]+")


def norm(text: str) -> str:
    """Нормализация для сравнения названий и тем."""
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def canonical_url(url: str) -> str:
    """Канонический URL: без схемы, www, завершающего слэша и регистра хоста."""
    value = (url or "").strip()
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^www\.", "", value, flags=re.IGNORECASE)
    value = value.rstrip("/")
    head, _, tail = value.partition("/")
    return f"{head.lower()}/{tail}" if tail else head.lower()


def group_observations(items: Sequence[dict[str, Any]]) -> dict[str, str]:
    """obs_id → group_id: объединение по общему каноническому URL или названию в пределах темы."""
    parent = {item["obs_id"]: item["obs_id"] for item in items}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    owner: dict[tuple[str, str, str], str] = {}
    for item in items:
        topic = norm(item["topic"])
        keys = [("t", topic, norm(item.get("title_auto") or item.get("title_ru", "")))]
        keys += [("u", topic, canonical_url(ev["url"])) for ev in item.get("evidence", []) if ev.get("url")]
        for key in keys:
            if key in owner:
                parent[find(item["obs_id"])] = find(owner[key])
            else:
                owner[key] = item["obs_id"]
    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        members[find(item["obs_id"])].append(item)
    mapping: dict[str, str] = {}
    for rows in members.values():
        anchor = min(norm(row.get("title_auto") or row.get("title_ru", "")) for row in rows)
        digest = hashlib.sha256(f"{norm(rows[0]['topic'])}|{anchor}".encode()).hexdigest()[:10]
        for row in rows:
            mapping[row["obs_id"]] = f"bgrp-{digest}"
    return mapping


def representative(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Представитель группы: самое позднее наблюдение с наибольшим числом свидетельств."""
    return max(rows, key=lambda row: (row["retrieved_at"], len(row.get("evidence", [])), -row.get("rank", 0)))
