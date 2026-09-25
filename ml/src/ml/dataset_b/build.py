"""Сборка датасета B v1: живые кандидаты прогонов + веб-наблюдения, silver-разметка, split, валидация.

Воспроизводимость: вход — файлы прогонов в корне проекта, `labels_silver_v1.jsonl`,
`web_observations_v1.jsonl`; выход детерминирован (сортировка, фиксированные правила split).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ml.dataset_b.grouping import canonical_url, group_observations, norm, representative
from ml.dataset_b.runs_parser import parse_all

DATASET_VERSION = "dsb-2026.09.24-v1"
ANNOTATOR = "claude-opus-5.5"
LABEL_KINDS = {
    "R": ("WEAK_SIGNAL_RELEVANT", 1),
    "N-OFF": ("OFF_TOPIC", 0),
    "N-GEN": ("GENERIC_CONCEPT", 0),
    "N-OVR": ("OVERVIEW", 0),
    "N-MAT": ("MATURE", 0),
    "N-NOI": ("NOISE", 0),
    "N-HYP": ("HYPE", 0),
    "N-FUND": ("ROUTINE_FUNDING", 0),
    "U": ("UNCERTAIN", None),
}
# Итоговый holdout фиксируется до обучения: две собственные темы и одна тема организаторов.
HOLDOUT_TOPICS = (
    "квантовые сенсоры для навигации",
    "перспективные решения в финтехе",
    "цифровые двойники в энергетике",
)
DEV_FOLDS = 5
DOMAIN_BY_TOPIC = {
    "edge-вычисления и периферийный инференс": "edge",
    "защита и безопасность систем искусственного интеллекта": "ai_security",
    "перспективные решения в финтехе": "fintech",
    "индустриальный искусственный интеллект на производстве": "industrial_ai",
    "инфраструктура для искусственного интеллекта и дата-центры": "ai_infrastructure",
    "робототехника и физический искусственный интеллект": "robotics",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _language(titles: list[str]) -> str:
    cyr = sum(1 for t in titles for ch in t if "а" <= ch.lower() <= "я")
    lat = sum(1 for t in titles for ch in t if "a" <= ch.lower() <= "z")
    if cyr and lat:
        return "mixed" if min(cyr, lat) / max(cyr, lat) > 0.2 else ("ru" if cyr > lat else "en")
    return "ru" if cyr else "en"


def _evidence_refs(evidence: list[dict[str, Any]], retrieved_at: str) -> list[dict[str, Any]]:
    refs = []
    for item in evidence:
        url = item.get("url", "")
        refs.append({
            "title": item.get("title", ""),
            "url": url,
            "canonical_url": canonical_url(url) if url else "",
            "source_key": item.get("source_key", "web"),
            "source_type": item.get("source_type", "OTHER"),
            "trust_level": item.get("trust_level", "LOW"),
            "language_code": item.get("language_code", ""),
            "published_at": item.get("published_at"),
            "retrieved_at": retrieved_at,
            "content_hash": _sha(f"{item.get('title', '')}|{url}"),
        })
    return refs


def _row(
    *, sample_id: str, origin: str, topic: str, title: str, evidence: list[dict[str, Any]], code: str,
    confidence: float, rationale: str, counterevidence: str, group_id: str, retrieved_at: str,
    access_status: str, observed_runtime: dict[str, Any] | None, n_observations: int, title_origin: str,
) -> dict[str, Any]:
    kind, label = LABEL_KINDS[code]
    refs = _evidence_refs(evidence, retrieved_at)
    domains = {ref["canonical_url"].split("/")[0] for ref in refs if ref["canonical_url"]}
    return {
        "sample_id": sample_id,
        "dataset_version": DATASET_VERSION,
        "origin": origin,
        "domain": DOMAIN_BY_TOPIC.get(topic, "own_topic"),
        "language": _language([title, *[ref["title"] for ref in refs]]),
        "topic": topic,
        "title": title,
        "title_origin": title_origin,
        "observation_text": " || ".join([title, *[ref["title"] for ref in refs]]),
        "label": label,
        "label_kind": kind,
        "label_code": code,
        "group_id": group_id,
        "technology_id": group_id,
        "evidence_as_of": retrieved_at[:10],
        "source_urls": [ref["url"] for ref in refs if ref["url"]],
        "canonical_urls": [ref["canonical_url"] for ref in refs if ref["canonical_url"]],
        "source_types": [ref["source_type"] for ref in refs],
        "source_independence": {"distinct_domains": len(domains), "evidence_count": len(refs),
                                "note": "домены посчитаны по URL; независимость первоисточников не проверялась"},
        "published_at": [ref["published_at"] for ref in refs],
        "event_date": None,
        "retrieved_at": retrieved_at,
        "evidence_refs": refs,
        "content_hashes": [ref["content_hash"] for ref in refs],
        "access_status": access_status,
        "annotation_method": "llm_assisted_silver",
        "annotator": ANNOTATOR,
        "model_version": ANNOTATOR,
        "annotation_confidence": confidence,
        "label_rationale": rationale,
        "counterevidence": counterevidence,
        "review_status": "pending_human_review",
        "split": "",
        "n_observations": n_observations,
        "license_or_usage_notes": "только метаданные: названия, URL, даты, типы источников; тексты не распространяются",
        "observed_runtime": observed_runtime or {},
    }


def assign_splits(
    rows: list[dict[str, Any]], holdout_topics: tuple[str, ...] = HOLDOUT_TOPICS
) -> dict[str, str]:
    """Тема → split: фиксированный holdout и DEV_FOLDS фолдов, сбалансированных по позитивам."""
    topics = sorted({row["topic"] for row in rows})
    mapping = {topic: "holdout" for topic in topics if topic in holdout_topics}
    positives = Counter(row["topic"] for row in rows if row["label"] == 1)
    sizes = Counter(row["topic"] for row in rows if row["label"] is not None)
    dev = [t for t in topics if t not in mapping]
    dev.sort(key=lambda t: (-positives[t], -sizes[t], _sha(t)))
    load = [[0, 0] for _ in range(DEV_FOLDS)]
    for topic in dev:
        fold = min(range(DEV_FOLDS), key=lambda k: (load[k][0], load[k][1], k))
        load[fold][0] += positives[topic]
        load[fold][1] += sizes[topic]
        mapping[topic] = f"dev_fold_{fold}"
    return mapping


def build(project_root: Path, data_dir: Path, run_files: list[str] | None = None) -> dict[str, Any]:
    """Строит все файлы датасета B и возвращает отчёт валидации.

    run_files — зафиксированный список прогонов версии (validation_report_v1.json → run_files).
    """
    raw, excluded, files = parse_all(project_root, run_files)
    if not files:
        raise FileNotFoundError(f"в {project_root} нет файлов прогонов: файлы датасета B не перезаписаны")
    items = [asdict(item) for item in raw]
    gmap = group_observations(items)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[gmap[item["obs_id"]]].append(item)
    labels = {row["group_id"]: row for row in map(json.loads, (data_dir / "labels_silver_v1.jsonl").read_text(
        encoding="utf-8").splitlines())}
    problems: list[str] = []
    missing = sorted(set(groups) - set(labels))
    orphan = sorted(set(labels) - set(groups))
    if missing:
        problems.append(f"группы без метки: {missing}")
    if orphan:
        problems.append(f"метки без группы: {orphan}")
    rows: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for gid, members in sorted(groups.items()):
        rep = representative(members)
        label = labels.get(gid, {"code": "U", "confidence": 0.0, "rationale": "нет метки"})
        rows.append(_row(
            sample_id=f"b-{gid[5:]}", origin="live_runs_2026_09", topic=rep["topic"], title=rep["title_auto"],
            evidence=rep["evidence"], code=label["code"], confidence=label["confidence"],
            rationale=label["rationale"], counterevidence="", group_id=gid, retrieved_at=rep["retrieved_at"],
            access_status="observed_in_run_log", observed_runtime={**rep["observed_runtime"],
                                                                    "run_id": rep["run_id"], "rank": rep["rank"],
                                                                    "title_ru": rep["title_ru"]},
            n_observations=len(members), title_origin="analyzer_title_auto",
        ))
        for member in members:
            duplicates.append({"group_id": gid, "obs_id": member["obs_id"], "run_id": member["run_id"],
                               "rank": member["rank"], "representative": member["obs_id"] == rep["obs_id"]})
    for line in (data_dir / "web_observations_v1.jsonl").read_text(encoding="utf-8").splitlines():
        web = json.loads(line)
        rows.append(_row(
            sample_id=f"b-{web['web_id'][5:]}", origin="web_search_2026_09_24", topic=web["topic"],
            title=web["title"], evidence=web["evidence"], code=web["code"], confidence=web["confidence"],
            rationale=web["rationale"], counterevidence=web.get("counterevidence", ""), group_id=web["web_id"],
            retrieved_at="2026-09-24T00:00:00+00:00", access_status="search_result_snippet",
            observed_runtime=None, n_observations=1, title_origin="annotator_keyphrase",
        ))
    split_of = assign_splits(rows)
    for row in rows:
        row["split"] = split_of[row["topic"]]
    problems.extend(_validate(rows))
    supervised = [row for row in rows if row["label"] is not None]
    uncertain = [row for row in rows if row["label"] is None]
    out = data_dir
    _write_jsonl(out / "dataset_b_v1.jsonl", supervised)
    _write_jsonl(out / "dataset_b_v1_uncertain.jsonl", uncertain)
    _write_jsonl(out / "duplicates_manifest_v1.jsonl", duplicates)
    _write_jsonl(out / "excluded_candidates_observed_v1.jsonl", excluded)
    with (out / "evidence_manifest_v1.csv").open("w", encoding="utf-8") as handle:
        handle.write("sample_id,position,source_type,trust_level,published_at,content_hash,url\n")
        for row in rows:
            for pos, ref in enumerate(row["evidence_refs"], 1):
                handle.write(f"{row['sample_id']},{pos},{ref['source_type']},{ref['trust_level']},"
                             f"{ref['published_at'] or ''},{ref['content_hash']},\"{ref['url']}\"\n")
    splits = {"dataset_version": DATASET_VERSION, "rule": "topic-level; holdout fixed before training",
              "holdout_topics": list(HOLDOUT_TOPICS), "topic_split": dict(sorted(split_of.items())),
              "sample_split": {row["sample_id"]: row["split"] for row in rows}}
    (out / "splits_v1.json").write_text(json.dumps(splits, ensure_ascii=False, indent=1), encoding="utf-8")
    report = _report(rows, supervised, uncertain, problems, files, excluded)
    report["input_sha256"] = {name: _sha((project_root / name).read_text(encoding="utf-8", errors="replace"))
                              for name in files}
    for name in ("labels_silver_v1.jsonl", "web_observations_v1.jsonl"):
        report["input_sha256"][name] = _sha((data_dir / name).read_text(encoding="utf-8"))
    for name in ("dataset_b_v1.jsonl", "dataset_b_v1_uncertain.jsonl", "splits_v1.json"):
        report.setdefault("output_sha256", {})[name] = _sha((out / name).read_text(encoding="utf-8"))
    (out / "validation_report_v1.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
    return report


REQUIRED = ("sample_id", "dataset_version", "origin", "domain", "language", "topic", "title", "observation_text",
            "label_kind", "group_id", "technology_id", "evidence_as_of", "source_urls", "canonical_urls",
            "published_at", "retrieved_at", "evidence_refs", "content_hashes", "access_status",
            "annotation_method", "annotator", "model_version", "annotation_confidence", "label_rationale",
            "review_status", "split", "license_or_usage_notes")


def _validate(rows: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for row in rows:
        sid = row["sample_id"]
        if sid in seen:
            problems.append(f"{sid}: дубликат sample_id")
        seen.add(sid)
        for key in REQUIRED:
            if key not in row or row[key] in ("",) and key not in ("counterevidence",):
                problems.append(f"{sid}: пустое поле {key}")
        if not row["evidence_refs"]:
            problems.append(f"{sid}: нет свидетельств")
        for ref in row["evidence_refs"]:
            if ref["url"] and not ref["url"].startswith(("http://", "https://")):
                problems.append(f"{sid}: некорректный URL {ref['url']}")
            if ref["published_at"] and ref["published_at"][:10] > row["evidence_as_of"]:
                problems.append(f"{sid}: дата публикации позже evidence_as_of (временная утечка)")
    per_group = defaultdict(set)
    for row in rows:
        per_group[row["group_id"]].add(row["split"])
    problems.extend(f"{gid}: группа в нескольких split" for gid, sp in per_group.items() if len(sp) > 1)
    return problems


def _report(rows, supervised, uncertain, problems, files, excluded) -> dict[str, Any]:  # noqa: ANN001
    by_split = defaultdict(Counter)
    for row in supervised:
        by_split[row["split"]][row["label"]] += 1
    lengths = defaultdict(list)
    for row in supervised:
        lengths[row["label"]].append(len(row["title"]))
    return {
        "dataset_version": DATASET_VERSION,
        "status": "ok" if not problems else "problems",
        "problems": problems,
        "run_files": files,
        "rows_total": len(rows),
        "supervised": len(supervised),
        "uncertain": len(uncertain),
        "labels": dict(Counter(row["label"] for row in supervised)),
        "label_kinds": dict(Counter(row["label_kind"] for row in rows)),
        "origins": dict(Counter(row["origin"] for row in rows)),
        "topics": dict(Counter(row["topic"] for row in rows)),
        "groups": len({row["group_id"] for row in rows}),
        "by_split": {k: {str(l): c for l, c in v.items()} for k, v in sorted(by_split.items())},
        "evidence_source_types": dict(Counter(t for row in supervised for t in row["source_types"])),
        "title_length_median_by_label": {str(k): sorted(v)[len(v) // 2] for k, v in lengths.items()},
        "excluded_candidates_observed": len(excluded),
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
