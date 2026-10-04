"""Снимок задания для режима А: чтение, проверка целостности, общие форматы."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "ws-benchmark-snapshot/1"
MANIFEST = "manifest.json"


def sha256_file(path: Path) -> str:
    """SHA-256 файла потоком."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_time(value: Any) -> datetime | None:
    """Время из JSON PostgreSQL (`row_to_json`) или ISO-строки; None для пустого значения."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Каталог снимка одного задания: манифест и файлы данных."""

    root: Path
    manifest: dict[str, Any]

    @property
    def name(self) -> str:
        """Имя каталога снимка."""
        return self.root.name

    @property
    def job_id(self) -> str:
        """Идентификатор задания на стенде."""
        return str(self.manifest.get("job_id", ""))

    @property
    def topic(self) -> str:
        """Текст запроса задания."""
        return str(self.manifest.get("topic", ""))

    def path(self, name: str) -> Path:
        """Путь к файлу снимка."""
        return self.root / name

    def has(self, name: str) -> bool:
        """Есть ли файл в снимке."""
        return (self.root / name).exists()

    def json(self, name: str) -> Any:
        """JSON-файл снимка."""
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def jsonl(self, name: str) -> list[dict[str, Any]]:
        """Файл JSON Lines; отсутствующий файл — пустой список."""
        path = self.root / name
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def verify(self) -> list[str]:
        """Проблемы целостности: нет файла, не совпала сумма, неизвестная версия формата."""
        problems: list[str] = []
        if self.manifest.get("schema") != SCHEMA:
            problems.append(f"формат снимка {self.manifest.get('schema')!r}, ожидается {SCHEMA!r}")
        for name, meta in sorted((self.manifest.get("files") or {}).items()):
            path = self.root / name
            if not path.is_file():
                problems.append(f"нет файла {name}")
            elif sha256_file(path) != meta.get("sha256"):
                problems.append(f"не совпала SHA-256 файла {name}")
        problems += [f"при выгрузке: {issue}" for issue in self.manifest.get("issues") or []]
        return problems


def load_snapshot(path: Path | str) -> Snapshot:
    """Снимок по каталогу (каталог должен содержать manifest.json)."""
    root = Path(path)
    return Snapshot(root=root, manifest=json.loads((root / MANIFEST).read_text(encoding="utf-8")))


def is_snapshot(root: Path) -> bool:
    """Каталог — снимок задания (а не, например, каталог модели analyzer со своим manifest.json)."""
    try:
        return json.loads((root / MANIFEST).read_text(encoding="utf-8")).get("schema") == SCHEMA
    except (OSError, ValueError, AttributeError):
        return False


def find_snapshots(paths: list[str]) -> list[Snapshot]:
    """Снимки по путям: каталог снимка или каталог, внутри которого лежат снимки (на любой глубине)."""
    found: list[Snapshot] = []
    for item in paths:
        root = Path(item)
        if is_snapshot(root):
            found.append(load_snapshot(root))
            continue
        found += [load_snapshot(m.parent) for m in sorted(root.rglob(MANIFEST)) if is_snapshot(m.parent)]
    return found


def write_manifest(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Дописывает в манифест суммы всех файлов каталога (кроме самого манифеста) и сохраняет его."""
    files = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != MANIFEST):
        files[str(path.relative_to(root))] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    manifest = dict(manifest, schema=SCHEMA, files=files)
    (root / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def live_items(snapshot: Snapshot) -> list[dict[str, Any]]:
    """Выдача живого задания: ранг, кандидат, название, уверенность, статус текста, документы источников."""
    job = snapshot.json("job.json")
    sources: dict[str, list[tuple[int, str]]] = {}
    for row in job.get("sources") or []:
        sources.setdefault(str(row["item_id"]), []).append((int(row.get("position") or 0), str(row["document_id"])))
    items = []
    for row in sorted(job.get("items") or [], key=lambda r: int(r["rank"])):
        items.append(normalize_item(row, [doc for _, doc in sorted(sources.get(str(row.get("item_id")), []))]))
    return items


def normalize_item(row: dict[str, Any], document_ids: list[str]) -> dict[str, Any]:
    """Карточка выдачи для сравнения живого задания и повтора."""
    return {"rank": int(row["rank"]), "candidate_id": str(row["candidate_id"]), "title_ru": str(row.get("title_ru") or ""),
            "score": round(float(row.get("score") or 0.0), 4), "narrative_status": str(row.get("narrative_status") or ""),
            "sources": list(document_ids)}
