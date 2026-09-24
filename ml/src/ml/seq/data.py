"""Наборы для последовательного обучения: A (организаторы + явно помеченные вспомогательные негативы) и B."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from analyzer.domain.features_v2 import EvidenceItem

from ml.dataset_b.grouping import canonical_url

A_ORIGIN_ORGANIZERS = "gpb_dataset_2026_09"
A_ORIGIN_AUX = "team_negatives_v1"
A_AS_OF_YEAR = 2026
TRAIN_SHARE, DEV_SHARE = 0.70, 0.15
NEAR_DUP_JACCARD = 0.6
LINK_JACCARD = 0.5
_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)


@dataclass(slots=True)
class Obs:
    """Наблюдение: тема (пусто для A), название, свидетельства, метка и служебные поля."""

    sample_id: str
    dataset: str
    origin: str
    topic: str
    title: str
    evidence: list[EvidenceItem]
    label: int
    group_id: str
    split: str
    domain: str
    label_kind: str
    as_of_year: int
    observed_score: float | None = None
    observed_query_sim: float | None = None
    urls: tuple[str, ...] = ()


def sha256_file(path: Path) -> str:
    """Контрольная сумма файла."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall(text.lower()) if len(token) >= 3}


def _jaccard(first: set[str], second: set[str]) -> float:
    return len(first & second) / len(first | second) if first and second else 0.0


def load_a(path: Path, seed: int) -> list[Obs]:
    """Строки A: только название (описание позитивов — объяснение метки и в признаки не идёт)."""
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    obs = [
        Obs(sample_id=row["row_id"], dataset="A", origin=row["origin"], topic="", title=row["title"], evidence=[],
            label=int(row["label"]), group_id=row["group_id"], split="", domain=row["domain_tag"],
            label_kind=row["label_kind"], as_of_year=A_AS_OF_YEAR,
            urls=tuple(canonical_url(url) for url in row.get("source_urls", [])))
        for row in rows
    ]
    parent = list(range(len(obs)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    tokens = [_tokens(item.title) for item in obs]
    for i in range(len(obs)):
        for j in range(i + 1, len(obs)):
            if _jaccard(tokens[i], tokens[j]) >= NEAR_DUP_JACCARD:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for index in range(len(obs)):
        groups.setdefault(find(index), []).append(index)
    strata: dict[tuple[int, str], list[list[int]]] = {}
    for members in groups.values():
        anchor = min(obs[i].group_id for i in members)
        gid = "agrp-" + hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:10]
        for i in members:
            obs[i].group_id = gid
        first = obs[min(members)]
        strata.setdefault((first.label, first.domain), []).append(members)
    for key in sorted(strata):
        glist = sorted(strata[key], key=lambda m: hashlib.sha256(f"{seed}|{obs[m[0]].group_id}".encode()).hexdigest())
        n_train, n_dev = round(len(glist) * TRAIN_SHARE), round(len(glist) * DEV_SHARE)
        for position, members in enumerate(glist):
            split = "train" if position < n_train else "dev" if position < n_train + n_dev else "test"
            for i in members:
                obs[i].split = split
    return obs


def load_b(path: Path) -> list[Obs]:
    """Строки датасета B (только с меткой; неопределённые лежат в отдельном файле)."""
    result: list[Obs] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("label") is None:
            continue
        evidence = [
            EvidenceItem(ref["title"], ref["source_type"], ref["trust_level"],
                         int(ref["published_at"][:4]) if ref.get("published_at") else None)
            for ref in row["evidence_refs"]
        ]
        observed = row.get("observed_runtime") or {}
        query_sim = observed.get("emb_sim_query")
        if query_sim is None:
            query_sim = (observed.get("features_by_name", {}).get("emb_sim_query") or [None])[0]
        score = observed.get("score")
        result.append(Obs(
            sample_id=row["sample_id"], dataset="B", origin=row["origin"], topic=row["topic"], title=row["title"],
            evidence=evidence, label=int(row["label"]), group_id=row["group_id"], split=row["split"],
            domain=row["domain"], label_kind=row["label_kind"], as_of_year=int(row["evidence_as_of"][:4]),
            observed_score=None if score is None else float(score),
            observed_query_sim=None if query_sim is None else float(query_sim),
            urls=tuple(row.get("canonical_urls", [])),
        ))
    return result


def cross_links(a_rows: list[Obs], b_rows: list[Obs]) -> list[tuple[str, str, str]]:
    """Связи A↔B: общий канонический URL или сходство названий (для исключения из A-train)."""
    links: set[tuple[str, str, str]] = set()
    by_url: dict[str, list[str]] = {}
    for row in a_rows:
        for url in row.urls:
            by_url.setdefault(url, []).append(row.sample_id)
    a_tokens = [(row.sample_id, _tokens(row.title)) for row in a_rows]
    for row in b_rows:
        for url in row.urls:
            for a_id in by_url.get(url, []):
                links.add((a_id, row.sample_id, "shared_url"))
        b_tok = _tokens(row.title)
        for a_id, tok in a_tokens:
            if _jaccard(tok, b_tok) >= LINK_JACCARD:
                links.add((a_id, row.sample_id, "title_overlap"))
    return sorted(links)
