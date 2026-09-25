"""Разбор результатов прогонов (analytics-*.txt, own-topics-*.json) в наблюдения-кандидаты.

Единица наблюдения совпадает с единицей решения analyzer: тема запроса + кластер свидетельств.
Оценки и признаки, посчитанные системой во время прогона, сохраняются только как диагностика
(observed_runtime) и в предиктивный вход не попадают.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_RUN_RE = re.compile(r"(\d{8})-(\d{6})")
_TOPIC_RE = re.compile(r"ТЕМА (\d+)/(\d+): (.+?) — запрос «(.+?)»")
_ITEM_RE = re.compile(r"^  #(\d+) \[([A-Z_]+) \| (\w+) \| скоринг ([\d.]+)%\] (.*)$")
_SRC_RE = re.compile(
    r"^\s+- \[([\w]+) \| ([A-Z_]+) \| ([A-Z]+) \| (\w*) \| ([\d-]*|None) \| ([A-Z_]+)\] (.*)$"
)
_FEAT_RE = re.compile(r"^\s{7}(.+?): значение (-?[\d.]+), вклад ([+-]?[\d.]+)")
_EXCL_RE = re.compile(r"^  \[([A-Z_]+) \| ([A-Z_]+) \| скоринг ([\d.]+)% \| документов (\d+)\] (.*)$")


@dataclass(slots=True)
class Evidence:
    """Доказательный документ карточки в том виде, в каком его видит analyzer."""

    title: str
    url: str
    source_key: str
    source_type: str
    trust_level: str
    language_code: str
    published_at: str | None
    snippet: str = ""
    summary_kind: str = ""


@dataclass(slots=True)
class RawCandidate:
    """Наблюдение-кандидат одного прогона."""

    obs_id: str
    run_id: str
    run_file: str
    retrieved_at: str
    topic: str
    domain_label: str
    job_id: str
    rank: int
    title_auto: str
    title_ru: str
    narrative_status: str
    evidence: list[Evidence] = field(default_factory=list)
    observed_runtime: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """Словарь для JSONL."""
        return asdict(self)


def _run_meta(path: Path) -> tuple[str, str]:
    match = _RUN_RE.search(path.name)
    if not match:
        return path.stem, ""
    day, time = match.groups()
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}T{time[:2]}:{time[2:4]}:{time[4:]}+00:00"
    return f"{day}-{time}", iso


def _obs_id(*parts: str) -> str:
    return "obs-" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def parse_analytics(path: Path) -> tuple[list[RawCandidate], list[dict[str, Any]]]:
    """Карточки выдачи и исключённые кандидаты одного файла analytics-*.txt."""
    run_id, retrieved_at = _run_meta(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    items: list[RawCandidate] = []
    excluded: list[dict[str, Any]] = []
    topic = domain = job_id = ""
    current: RawCandidate | None = None
    mode = ""
    pending_src: Evidence | None = None
    for line in lines:
        tmatch = _TOPIC_RE.search(line)
        if tmatch:
            domain, topic = tmatch.group(3).strip(), tmatch.group(4).strip()
            current, mode = None, ""
            continue
        if line.startswith("job_id:"):
            job_id = line.split()[1]
            continue
        if line.startswith("ВЫДАЧА:"):
            mode = "items"
            continue
        if line.startswith("ИСКЛЮЧЁННЫЕ КАНДИДАТЫ"):
            mode, current = "excluded", None
            continue
        if mode == "items":
            imatch = _ITEM_RE.match(line)
            if imatch:
                rank, status, band, score, title = imatch.groups()
                current = RawCandidate(
                    obs_id=_obs_id(run_id, job_id, rank, title),
                    run_id=run_id, run_file=path.name, retrieved_at=retrieved_at, topic=topic,
                    domain_label=domain, job_id=job_id, rank=int(rank), title_auto="", title_ru=title.strip(),
                    narrative_status=status,
                    observed_runtime={"score": float(score) / 100.0, "confidence_band": band, "features": {}},
                )
                items.append(current)
                continue
            if current is None:
                continue
            stripped = line.strip()
            if stripped.startswith("автоназвание кластера:"):
                current.title_auto = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("близость к запросу (emb_sim_query):"):
                current.observed_runtime["emb_sim_query"] = float(stripped.rsplit(":", 1)[1])
            elif stripped.startswith("решение модели:"):
                current.observed_runtime["decision_explanation"] = stripped.split(":", 1)[1].strip()[:300]
            else:
                fmatch = _FEAT_RE.match(line)
                smatch = _SRC_RE.match(line)
                if smatch:
                    key, stype, trust, lang, date, kind, title = smatch.groups()
                    pending_src = Evidence(
                        title=title.strip(), url="", source_key=key, source_type=stype, trust_level=trust,
                        language_code=lang, published_at=None if date in ("", "None") else date,
                        summary_kind=kind,
                    )
                    current.evidence.append(pending_src)
                elif pending_src is not None and stripped.startswith("http") and not pending_src.url:
                    pending_src.url = stripped
                elif pending_src is not None and stripped.startswith("резюме:") and pending_src.summary_kind in (
                    "EXTRACTIVE", "ORIGINAL_RU"
                ):
                    pending_src.snippet = stripped.split(":", 1)[1].strip()
                elif fmatch and not stripped.startswith("-"):
                    label, value, contribution = fmatch.groups()
                    current.observed_runtime["features"][label] = [float(value), float(contribution)]
        elif mode == "excluded":
            ematch = _EXCL_RE.match(line)
            if ematch:
                decision, reason, score, docs, title = ematch.groups()
                excluded.append({
                    "run_id": run_id, "topic": topic, "domain_label": domain, "job_id": job_id,
                    "decision": decision, "reason": reason, "score": float(score) / 100.0,
                    "documents": int(docs), "title": title.strip(),
                })
    return items, excluded


def parse_own_topics(path: Path) -> list[RawCandidate]:
    """Карточки файла own-topics-*.json (полные данные API результата)."""
    run_id, retrieved_at = _run_meta(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items: list[RawCandidate] = []
    for block in payload:
        topic = str(block.get("topic") or block.get("job", {}).get("query_text", ""))
        job = block.get("job", {})
        for card in block.get("cards", []) or []:
            evidence = [
                Evidence(
                    title=str(src.get("title", "")), url=str(src.get("url", "")),
                    source_key=str(src.get("source_key", "")), source_type=str(src.get("source_type", "")),
                    trust_level=str(src.get("trust_level", "")), language_code=str(src.get("language_code", "")),
                    published_at=(str(src["published_at"])[:10] if src.get("published_at") else None),
                    snippet=str(src.get("snippet") or ""), summary_kind=str(src.get("summary_kind", "")),
                )
                for src in card.get("sources", []) or []
            ]
            features = {f["feature_name"]: [f.get("value"), f.get("contribution")] for f in card.get("features", [])}
            items.append(
                RawCandidate(
                    obs_id=_obs_id(run_id, str(card.get("item_id", "")), str(card.get("rank", ""))),
                    run_id=run_id, run_file=path.name,
                    retrieved_at=str(job.get("finished_at") or retrieved_at), topic=topic, domain_label="own_topic",
                    job_id=str(job.get("job_id", "")), rank=int(card.get("rank", 0) or 0),
                    title_auto=str(card.get("title_auto", "")), title_ru=str(card.get("title_ru", "")),
                    narrative_status=str(card.get("narrative_status", "")), evidence=evidence,
                    observed_runtime={"score": card.get("score"), "confidence_band": card.get("confidence_band"),
                                      "features_by_name": features},
                )
            )
    return items


def parse_all(
    root: Path, names: list[str] | None = None
) -> tuple[list[RawCandidate], list[dict[str, Any]], list[str]]:
    """Прогоны каталога проекта; третий элемент — список разобранных файлов.

    names — зафиксированный список файлов версии датасета (новые прогоны в корне её не меняют);
    None — все analytics-*.txt, затем все own-topics-*.json.
    """
    if names is None:
        paths = sorted(root.glob("analytics-*.txt")) + sorted(root.glob("own-topics-*.json"))
    else:
        paths = [root / name for name in names]
        missing = [path.name for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"в {root} нет файлов прогонов: {missing}")
    items: list[RawCandidate] = []
    excluded: list[dict[str, Any]] = []
    files: list[str] = []
    for path in paths:
        if path.name.startswith("analytics-") and path.suffix == ".txt":
            found, skipped = parse_analytics(path)
            items.extend(found)
            excluded.extend(skipped)
        elif path.name.startswith("own-topics-") and path.suffix == ".json":
            items.extend(parse_own_topics(path))
        else:
            raise ValueError(f"неизвестный тип файла прогона: {path.name}")
        files.append(path.name)
    return items, excluded, files
