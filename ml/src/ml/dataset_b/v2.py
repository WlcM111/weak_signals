"""Датасет B v2: двойная экспертная разметка, новые holdout-темы, сборка без перезаписи файлов v1.

Запуск из корня проекта (только стандартная библиотека Python):
    PYTHONPATH=ml/src python3 -m ml.dataset_b.v2 <команда>

Порядок работы:
  topics-check  файлы новых тем (holdout_topics.txt, extra_topics.txt) — до живых прогонов;
  export        фиксирует прогоны, holdout-темы и группы (manifest.json) и выгружает слепые листы
                expert1.csv и expert2.csv: без silver-меток, производственного score и признаков;
  agreement     каппа Коэна по двум листам и лист арбитража arbitration.csv для расхождений;
  merge         итоговые экспертные метки labels_v2.jsonl (согласие двух экспертов или решение арбитра);
  build         датасет B v2 из зафиксированных прогонов: экспертная метка, иначе silver v1 по составу
                группы; группы holdout-тем — только с экспертной меткой; файлы v1 не затрагиваются;
  export-runs   слепой лист для быстрого замера точности выдачи нескольких прогонов (в датасет не идёт);
  precision     точность размеченной выдачи: доля R среди размеченных без U, 95 % ДИ Уилсона.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ml.dataset_b.build import ANNOTATOR, LABEL_KINDS, _row, _validate, _write_jsonl, assign_splits
from ml.dataset_b.grouping import group_observations, norm, representative
from ml.dataset_b.runs_parser import parse_all, parse_analytics

DATASET_VERSION_V2 = "dsb-v2"
MANIFEST_FORMAT = "ws-dsb-v2-labeling-1"
V2_DIR = "v2"
LABELING_DIR = "labeling"
HOLDOUT_FILE = "holdout_topics.txt"
EXTRA_FILE = "extra_topics.txt"
MANIFEST_FILE = "manifest.json"
EXPERT_SHEETS = ("expert1.csv", "expert2.csv")
ARBITRATION_SHEET = "arbitration.csv"
LABELS_FILE = "labels_v2.jsonl"
SHEET_FIELDS = ["group_id", "queue", "topic", "title", "cluster_title", "evidence", "urls", "label", "comment"]
ARBITRATION_FIELDS = ["group_id", "topic", "title", "cluster_title", "evidence", "urls", "expert1_label",
                      "expert1_comment", "expert2_label", "expert2_comment", "label", "comment"]
CODES = tuple(LABEL_KINDS)
PRIORITY_SILVER = frozenset({"R", "U"})
MAX_EVIDENCE = 5
EXPERT_METHOD = "expert_double_annotation"
SILVER_METHOD = "llm_assisted_silver"
# Код, набранный в русской раскладке (например, «к» вместо «R»), переводится по положению клавиш.
_RU_TO_EN = str.maketrans("йцукенгшщзхъфывапролджэячсмитьбю", "qwertyuiop[]asdfghjkl;'zxcvbnm,.")
_CYRILLIC_RE = re.compile("[а-яё]")


class DatasetV2Error(RuntimeError):
    """Нарушение порядка работы или целостности входных файлов."""


# ---------------------------------------------------------------- общие утилиты


def _sha_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, default=str)


def normalize_code(value: str) -> tuple[str | None, bool]:
    """Код метки из ячейки: (код | None для пустой | "?" для неверной, была ли исправлена раскладка)."""
    text = (value or "").strip()
    if not text:
        return None, False
    fixed = bool(_CYRILLIC_RE.search(text.lower()))
    if fixed:
        text = text.lower().translate(_RU_TO_EN)
    code = text.upper().replace("_", "-").replace(" ", "")
    return (code if code in CODES else "?"), fixed


def read_sheet(path: Path) -> list[dict[str, str]]:
    """Строки CSV после табличного редактора: UTF-8 (в т. ч. с BOM) или cp1251; «,», «;» или табуляция.

    Строкой заголовка считается первая из первых пяти, где есть колонка label: Numbers при экспорте
    может добавить строку с именем таблицы.
    """
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1251 декодирует почти любые байты
        raise DatasetV2Error(f"{path}: не удалось прочитать кодировку; сохраните как CSV UTF-8")
    lines = text.splitlines()
    header_index = next((i for i, line in enumerate(lines[:5]) if "label" in line.lower()), None)
    if header_index is None:
        raise DatasetV2Error(f"{path}: нет строки заголовка с колонкой label")
    header = lines[header_index]
    delimiter = max((",", ";", "\t"), key=header.count)
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])), delimiter=delimiter)
    rows = []
    for row in reader:
        clean = {(key or "").strip().lower(): (value or "").strip() for key, value in row.items() if key}
        if any(clean.values()):
            rows.append(clean)
    return rows


def _write_sheet(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    """CSV с BOM: Excel для Mac без BOM открывает кириллицу неверно, Numbers читает оба варианта."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def cohen_kappa(first: list[str], second: list[str]) -> float | None:
    """Каппа Коэна двух разметчиков на одних и тех же объектах; None, если ожидаемое согласие равно 1."""
    n = len(first)
    if n == 0 or n != len(second):
        return None
    observed = sum(a == b for a, b in zip(first, second, strict=True)) / n
    count_a, count_b = Counter(first), Counter(second)
    expected = sum(count_a[c] * count_b[c] for c in set(first) | set(second)) / (n * n)
    return None if expected >= 1.0 else (observed - expected) / (1.0 - expected)


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float | None, float | None]:
    """95 % доверительный интервал Уилсона для доли."""
    if total == 0:
        return None, None
    p = successes / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return max(0.0, (centre - half) / denom), min(1.0, (centre + half) / denom)


def _paths(b_dir: Path) -> dict[str, Path]:
    v2 = Path(b_dir) / V2_DIR
    lab = v2 / LABELING_DIR
    return {"b": Path(b_dir), "v2": v2, "lab": lab, "manifest": lab / MANIFEST_FILE,
            "labels": v2 / LABELS_FILE, "holdout": v2 / HOLDOUT_FILE, "extra": v2 / EXTRA_FILE}


def _read_topics(path: Path) -> list[str]:
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def v1_topics(b_dir: Path) -> set[str]:
    """Темы датасета B v1 (размеченные и неопределённые строки)."""
    rows = _jsonl(Path(b_dir) / "dataset_b_v1.jsonl") + _jsonl(Path(b_dir) / "dataset_b_v1_uncertain.jsonl")
    return {row["topic"] for row in rows}


def _run_files(project_root: Path) -> list[str]:
    root = Path(project_root)
    return [p.name for p in sorted(root.glob("analytics-*.txt"))] + [
        p.name for p in sorted(root.glob("own-topics-*.json"))]


def _groups(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    gmap = group_observations(items)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[gmap[item["obs_id"]]].append(item)
    return dict(groups)


def _evidence_text(evidence: list[dict[str, Any]]) -> tuple[str, str]:
    lines, urls = [], []
    for ev in evidence[:MAX_EVIDENCE]:
        lines.append(" | ".join(str(ev.get(k) or "—") for k in ("source_key", "source_type", "trust_level",
                                                                 "published_at", "title")))
        if ev.get("url"):
            urls.append(str(ev["url"]))
    return " || ".join(lines), " ".join(urls)


def _order_key(group_id: str) -> str:
    return hashlib.sha256(f"ws-dsb-v2|{group_id}".encode()).hexdigest()


# ---------------------------------------------------------------- темы


def topics_check(b_dir: Path) -> dict[str, Any]:
    """Проверка файлов новых тем до живых прогонов: пустые, дубли, пересечения с B v1 и между файлами."""
    paths = _paths(b_dir)
    problems: list[str] = []
    if not paths["holdout"].is_file():
        raise DatasetV2Error(f"нет файла {paths['holdout']}")
    holdout = _read_topics(paths["holdout"])
    extra = _read_topics(paths["extra"]) if paths["extra"].is_file() else []
    known = {norm(t): t for t in v1_topics(b_dir)}
    if len(holdout) < 3:
        problems.append(f"holdout-тем {len(holdout)}: нужно не меньше 3")
    for name, topics in (("holdout", holdout), ("extra", extra)):
        seen: set[str] = set()
        for topic in topics:
            key = norm(topic)
            if topic.startswith("#"):
                problems.append(f"{name}: строка «{topic}» — в файле тем не должно быть комментариев")
            if key in seen:
                problems.append(f"{name}: тема «{topic}» повторяется")
            seen.add(key)
            if key in known:
                problems.append(f"{name}: тема «{topic}» уже есть в датасете B v1 («{known[key]}»)")
    overlap = {norm(t) for t in holdout} & {norm(t) for t in extra}
    if overlap:
        problems.append(f"темы есть и в holdout, и в extra: {sorted(overlap)}")
    return {"status": "ok" if not problems else "problems", "holdout_topics": holdout, "extra_topics": extra,
            "problems": problems, "v1_topics": sorted(known.values())}


# ---------------------------------------------------------------- выгрузка для экспертов


def _silver_index(b_dir: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    obs_to_v1 = {row["obs_id"]: row["group_id"] for row in _jsonl(Path(b_dir) / "duplicates_manifest_v1.jsonl")}
    labels = {row["group_id"]: row for row in _jsonl(Path(b_dir) / "labels_silver_v1.jsonl")}
    return obs_to_v1, labels


def _silver_for(members: list[str], obs_to_v1: dict[str, str],
                labels: dict[str, dict[str, Any]]) -> tuple[dict[str, Any] | None, list[str]]:
    """Silver-метка v1 группы v2 по составу: одна метка — переносится; несколько разных — конфликт."""
    old = sorted({obs_to_v1[m] for m in members if m in obs_to_v1})
    entries = [labels[g] for g in old if g in labels]
    codes = sorted({entry["code"] for entry in entries})
    if len(codes) == 1:
        return {"code": codes[0], "confidence": min(float(e["confidence"]) for e in entries),
                "rationale": " | ".join(dict.fromkeys(e["rationale"] for e in entries)), "v1_groups": old}, []
    return None, codes


def export_sheets(project_root: Path, b_dir: Path, *, force: bool = False) -> dict[str, Any]:
    """Фиксирует состав датасета v2 и выгружает два одинаковых слепых листа разметки."""
    paths = _paths(b_dir)
    check = topics_check(b_dir)
    if check["problems"]:
        raise DatasetV2Error("файлы тем не прошли проверку: " + "; ".join(check["problems"]))
    targets = [paths["manifest"], *(paths["lab"] / name for name in EXPERT_SHEETS)]
    if not force and any(p.exists() for p in targets):
        raise DatasetV2Error(f"листы уже выгружены в {paths['lab']}: повторная выгрузка затёрла бы разметку")
    run_files = _run_files(project_root)
    if not run_files:
        raise DatasetV2Error(f"в {project_root} нет analytics-*.txt и own-topics-*.json")
    raw, _excluded, files = parse_all(Path(project_root), run_files)
    items = [asdict(item) for item in raw]
    groups = _groups(items)
    by_norm: dict[str, set[str]] = defaultdict(set)
    for members in groups.values():
        by_norm[norm(members[0]["topic"])].add(members[0]["topic"])
    holdout_exact: list[str] = []
    for topic in check["holdout_topics"]:
        found = sorted(by_norm.get(norm(topic), set()))
        if not found:
            raise DatasetV2Error(f"по holdout-теме «{topic}» нет ни одной карточки в прогонах: запустите её")
        holdout_exact.extend(found)
    holdout_set = set(holdout_exact)
    obs_to_v1, labels = _silver_index(b_dir)
    records: dict[str, dict[str, Any]] = {}
    sheet_rows: list[dict[str, Any]] = []
    for gid, members in groups.items():
        rep = representative(members)
        silver, conflict = _silver_for([m["obs_id"] for m in members], obs_to_v1, labels)
        holdout = rep["topic"] in holdout_set
        required = holdout or silver is None
        queue = 1 if required or (silver and silver["code"] in PRIORITY_SILVER) else 2
        evidence, urls = _evidence_text(rep["evidence"])
        records[gid] = {"origin": "live", "topic": rep["topic"], "members": sorted(m["obs_id"] for m in members),
                        "runs": sorted({m["run_id"] for m in members}), "holdout": holdout, "silver": silver,
                        "silver_conflict": conflict, "required": required, "queue": queue}
        sheet_rows.append({"group_id": gid, "queue": queue, "topic": rep["topic"],
                           "title": rep["title_ru"] or rep["title_auto"], "cluster_title": rep["title_auto"],
                           "evidence": evidence, "urls": urls, "label": "", "comment": ""})
    for web in _jsonl(paths["b"] / "web_observations_v1.jsonl"):
        gid = web["web_id"]
        silver = {"code": web["code"], "confidence": float(web["confidence"]), "rationale": web["rationale"],
                  "v1_groups": [gid]}
        queue = 1 if web["code"] in PRIORITY_SILVER else 2
        evidence, urls = _evidence_text(web["evidence"])
        records[gid] = {"origin": "web", "topic": web["topic"], "members": [gid], "runs": [], "holdout": False,
                        "silver": silver, "silver_conflict": [], "required": False, "queue": queue}
        sheet_rows.append({"group_id": gid, "queue": queue, "topic": web["topic"], "title": web["title"],
                           "cluster_title": "", "evidence": evidence, "urls": urls, "label": "", "comment": ""})
    sheet_rows.sort(key=lambda row: (row["queue"], _order_key(row["group_id"])))
    manifest = {
        "format": MANIFEST_FORMAT,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_files": [{"name": name, "sha256": _sha_file(Path(project_root) / name)} for name in files],
        "holdout_topics": sorted(holdout_set),
        "holdout_file_sha256": _sha_file(paths["holdout"]),
        "extra_topics": check["extra_topics"],
        "v1_inputs": {name: _sha_file(paths["b"] / name) for name in (
            "labels_silver_v1.jsonl", "duplicates_manifest_v1.jsonl", "web_observations_v1.jsonl")},
        "groups": dict(sorted(records.items())),
    }
    paths["lab"].mkdir(parents=True, exist_ok=True)
    paths["manifest"].write_text(_dump(manifest), encoding="utf-8")
    for name in EXPERT_SHEETS:
        _write_sheet(paths["lab"] / name, SHEET_FIELDS, sheet_rows)
    found = {obs_to_v1[m] for rec in records.values() for m in rec["members"] if m in obs_to_v1}
    return {**_export_summary(manifest), "v1_labels": len(labels), "v1_labels_found": len(set(labels) & found),
            "v1_labels_lost": sorted(set(labels) - found)}


def _export_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    groups = manifest["groups"].values()
    holdout = Counter(g["topic"] for g in groups if g["holdout"])
    return {
        "run_files": len(manifest["run_files"]), "groups": len(manifest["groups"]),
        "live": sum(g["origin"] == "live" for g in groups), "web": sum(g["origin"] == "web" for g in groups),
        "queue_1": sum(g["queue"] == 1 for g in groups), "queue_2": sum(g["queue"] == 2 for g in groups),
        "required_expert": sum(g["required"] for g in groups),
        "without_silver": sum(g["silver"] is None for g in groups),
        "silver_conflicts": sum(bool(g["silver_conflict"]) for g in groups),
        "holdout_groups_by_topic": dict(sorted(holdout.items())),
    }


# ---------------------------------------------------------------- согласие и арбитраж


def _load_manifest(paths: dict[str, Path]) -> dict[str, Any]:
    if not paths["manifest"].is_file():
        raise DatasetV2Error(f"нет {paths['manifest']}: сначала команда export")
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    if manifest.get("format") != MANIFEST_FORMAT:
        raise DatasetV2Error("manifest.json неизвестного формата")
    return manifest


def read_labels(path: Path, known: set[str]) -> tuple[dict[str, tuple[str, str]], list[str], list[str]]:
    """{group_id: (код, комментарий)} из листа; ошибки (неверный код, чужой id, дубль) и исправленные раскладки."""
    result: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    fixed: list[str] = []
    for row in read_sheet(path):
        gid = row.get("group_id", "")
        if not gid:
            continue
        if gid not in known:
            problems.append(f"{path.name}: неизвестный group_id {gid}")
            continue
        code, was_fixed = normalize_code(row.get("label", ""))
        if code is None:
            continue
        if code == "?":
            problems.append(f"{path.name}: {gid}: неверный код «{row.get('label')}» (допустимы {', '.join(CODES)})")
            continue
        if was_fixed:
            fixed.append(f"{path.name}: {gid}: «{row.get('label')}» → {code}")
        if gid in result:
            problems.append(f"{path.name}: {gid} встречается дважды")
        result[gid] = (code, row.get("comment", ""))
    return result, problems, fixed


def agreement(b_dir: Path, *, force: bool = False) -> dict[str, Any]:
    """Каппа Коэна по двум листам; расхождения — в arbitration.csv для третьего эксперта."""
    paths = _paths(b_dir)
    manifest = _load_manifest(paths)
    known = set(manifest["groups"])
    first, p1, f1 = read_labels(paths["lab"] / EXPERT_SHEETS[0], known)
    second, p2, f2 = read_labels(paths["lab"] / EXPERT_SHEETS[1], known)
    problems = p1 + p2
    if problems:
        return {"status": "problems", "problems": problems, "layout_fixed": f1 + f2}
    both = sorted(set(first) & set(second))
    codes_1 = [first[g][0] for g in both]
    codes_2 = [second[g][0] for g in both]
    binary = [g for g in both if first[g][0] != "U" and second[g][0] != "U"]
    disagree = [g for g in both if first[g][0] != second[g][0]]
    required_missing = sorted(g for g, rec in manifest["groups"].items()
                              if rec["required"] and not (g in first and g in second))
    arbitration_path = paths["lab"] / ARBITRATION_SHEET
    if arbitration_path.exists() and not force:
        filled = [row for row in read_sheet(arbitration_path) if (row.get("label") or "").strip()]
        if filled:
            raise DatasetV2Error(f"{arbitration_path} уже заполнен арбитром: повторный расчёт затёр бы его")
    sheet = {row["group_id"]: row for row in read_sheet(paths["lab"] / EXPERT_SHEETS[0])}
    rows = []
    for gid in sorted(disagree, key=lambda g: (first[g][0] == "R") == (second[g][0] == "R")):
        base = sheet.get(gid, {})
        rows.append({"group_id": gid, "topic": base.get("topic", ""), "title": base.get("title", ""),
                     "cluster_title": base.get("cluster_title", ""), "evidence": base.get("evidence", ""),
                     "urls": base.get("urls", ""), "expert1_label": first[gid][0], "expert1_comment": first[gid][1],
                     "expert2_label": second[gid][0], "expert2_comment": second[gid][1], "label": "", "comment": ""})
    _write_sheet(arbitration_path, ARBITRATION_FIELDS, rows)
    report = {
        "status": "ok",
        "labeled_by_both": len(both), "only_expert1": len(set(first) - set(second)),
        "only_expert2": len(set(second) - set(first)),
        "agreement_share": round(sum(a == b for a, b in zip(codes_1, codes_2, strict=True)) / len(both), 4)
        if both else None,
        "kappa_codes": _round(cohen_kappa(codes_1, codes_2)),
        "kappa_binary_R_vs_negative": _round(cohen_kappa(
            ["R" if first[g][0] == "R" else "N" for g in binary],
            ["R" if second[g][0] == "R" else "N" for g in binary])),
        "binary_rows_without_U": len(binary),
        "disagreements": len(disagree),
        "disagreements_on_R": sum((first[g][0] == "R") != (second[g][0] == "R") for g in disagree),
        "confusion": dict(sorted(Counter(f"{first[g][0]}|{second[g][0]}" for g in both).items())),
        "required_not_labeled_by_both": required_missing,
        "layout_fixed": f1 + f2,
        "arbitration_sheet": str(arbitration_path),
    }
    (paths["lab"] / "agreement_report.json").write_text(_dump(report), encoding="utf-8")
    return report


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def merge(b_dir: Path) -> dict[str, Any]:
    """Итоговые экспертные метки: совпадение двух экспертов или решение арбитра по расхождению."""
    paths = _paths(b_dir)
    manifest = _load_manifest(paths)
    known = set(manifest["groups"])
    first, p1, _ = read_labels(paths["lab"] / EXPERT_SHEETS[0], known)
    second, p2, _ = read_labels(paths["lab"] / EXPERT_SHEETS[1], known)
    arbitration_path = paths["lab"] / ARBITRATION_SHEET
    arbiter, p3, _ = (read_labels(arbitration_path, known) if arbitration_path.exists() else ({}, [], []))
    problems = p1 + p2 + p3
    entries, unresolved, single = [], [], []
    for gid in sorted(known):
        one, two = first.get(gid), second.get(gid)
        if one and two:
            votes = {"expert1": one[0], "expert2": two[0]}
            comments = [one[1], two[1]]
            if one[0] == two[0]:
                final, confidence, annotators = one[0], 1.0, ["expert1", "expert2"]
            elif gid in arbiter:
                final = arbiter[gid][0]
                votes["arbiter"] = final
                comments.append(arbiter[gid][1])
                confidence = round(sum(v == final for v in votes.values()) / 3, 3)
                annotators = ["expert1", "expert2", "arbiter"]
            else:
                unresolved.append(gid)
                continue
            entries.append({"group_id": gid, "code": final, "confidence": confidence,
                            "rationale": " | ".join(c for c in dict.fromkeys(comments) if c) or "экспертная метка",
                            "review_status": "reviewed", "annotation_method": EXPERT_METHOD,
                            "annotators": annotators, "votes": votes})
        elif one or two:
            single.append(gid)
    if unresolved:
        problems.append(f"расхождения без решения арбитра ({len(unresolved)}): {unresolved}")
    if problems:
        return {"status": "problems", "problems": problems}
    _write_jsonl(paths["labels"], entries)
    required_missing = sorted(g for g, rec in manifest["groups"].items()
                              if rec["required"] and g not in {e["group_id"] for e in entries})
    return {"status": "ok", "labels_file": str(paths["labels"]), "reviewed": len(entries),
            "agreed": sum(len(e["votes"]) == 2 for e in entries),
            "arbitrated": sum(len(e["votes"]) == 3 for e in entries),
            "by_code": dict(sorted(Counter(e["code"] for e in entries).items())),
            "labeled_by_one_expert_only_not_used": single, "required_still_missing": required_missing}


# ---------------------------------------------------------------- сборка v2


def build_v2(project_root: Path, b_dir: Path) -> dict[str, Any]:
    """Датасет B v2 из прогонов manifest.json; файлы датасета пишутся только при отсутствии проблем."""
    paths = _paths(b_dir)
    manifest = _load_manifest(paths)
    problems: list[str] = []
    for entry in manifest["run_files"]:
        path = Path(project_root) / entry["name"]
        if not path.is_file():
            raise DatasetV2Error(f"нет файла прогона {path}: он был в выгрузке для экспертов")
        if _sha_file(path) != entry["sha256"]:
            raise DatasetV2Error(f"файл прогона {entry['name']} изменён после выгрузки для экспертов")
    if _sha_file(paths["holdout"]) != manifest["holdout_file_sha256"]:
        raise DatasetV2Error(f"{paths['holdout']} изменён после выгрузки: holdout нельзя менять после разметки")
    for name, sha in manifest["v1_inputs"].items():
        if _sha_file(paths["b"] / name) != sha:
            raise DatasetV2Error(f"{name} изменён после выгрузки для экспертов")
    raw, excluded, files = parse_all(Path(project_root), [e["name"] for e in manifest["run_files"]])
    items = [asdict(item) for item in raw]
    groups = _groups(items)
    live = {gid for gid, rec in manifest["groups"].items() if rec["origin"] == "live"}
    if set(groups) != live:
        raise DatasetV2Error("группы прогонов не совпадают с manifest.json: пересоберите выгрузку")
    expert = {row["group_id"]: row for row in _jsonl(paths["labels"])} if paths["labels"].is_file() else {}
    rows: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    sources = Counter()
    for gid, members in sorted(groups.items()):
        rep = representative(members)
        label = _resolve(gid, manifest["groups"][gid], expert, problems)
        if label is None:
            continue
        sources[label["annotation_method"]] += 1
        row = _row(sample_id=f"b-{gid[5:]}", origin=f"live_runs_{rep['run_id'][:4]}_{rep['run_id'][4:6]}",
                   topic=rep["topic"], title=rep["title_auto"], evidence=rep["evidence"], code=label["code"],
                   confidence=label["confidence"], rationale=label["rationale"], counterevidence="",
                   group_id=gid, retrieved_at=rep["retrieved_at"], access_status="observed_in_run_log",
                   observed_runtime={**rep["observed_runtime"], "run_id": rep["run_id"], "rank": rep["rank"],
                                     "title_ru": rep["title_ru"]},
                   n_observations=len(members), title_origin="analyzer_title_auto")
        rows.append(_finish(row, label))
        duplicates.extend({"group_id": gid, "obs_id": m["obs_id"], "run_id": m["run_id"], "rank": m["rank"],
                           "representative": m["obs_id"] == rep["obs_id"]} for m in members)
    for web in _jsonl(paths["b"] / "web_observations_v1.jsonl"):
        gid = web["web_id"]
        label = _resolve(gid, manifest["groups"][gid], expert, problems)
        if label is None:
            continue
        sources[label["annotation_method"]] += 1
        row = _row(sample_id=f"b-{gid[5:]}", origin="web_search_2026_09_24", topic=web["topic"],
                   title=web["title"], evidence=web["evidence"], code=label["code"],
                   confidence=label["confidence"], rationale=label["rationale"],
                   counterevidence=web.get("counterevidence", ""), group_id=gid,
                   retrieved_at="2026-09-24T00:00:00+00:00", access_status="search_result_snippet",
                   observed_runtime=None, n_observations=1, title_origin="annotator_keyphrase")
        rows.append(_finish(row, label))
    holdout_topics = tuple(manifest["holdout_topics"])
    split_of = assign_splits(rows, holdout_topics=holdout_topics)
    for row in rows:
        row["split"] = split_of[row["topic"]]
    problems.extend(_validate(rows))
    supervised = [r for r in rows if r["label"] is not None]
    uncertain = [r for r in rows if r["label"] is None]
    holdout_rows = [r for r in supervised if r["split"] == "holdout"]
    for topic in holdout_topics:
        if not any(r["topic"] == topic for r in holdout_rows):
            problems.append(f"в holdout-теме «{topic}» нет ни одной размеченной строки (всё U)")
    if holdout_rows and not any(r["label"] == 1 for r in holdout_rows):
        problems.append("в holdout нет ни одного позитива: PR-AUC на holdout не определена")
    if not any(r["label"] == 1 for r in supervised if r["split"] != "holdout"):
        problems.append("в dev нет ни одного позитива")
    report = _report_v2(rows, supervised, uncertain, problems, files, excluded, sources, holdout_topics)
    paths["v2"].mkdir(parents=True, exist_ok=True)
    if not problems:
        _write_jsonl(paths["v2"] / "dataset_b_v2.jsonl", supervised)
        _write_jsonl(paths["v2"] / "dataset_b_v2_uncertain.jsonl", uncertain)
        _write_jsonl(paths["v2"] / "duplicates_manifest_v2.jsonl", duplicates)
        _write_jsonl(paths["v2"] / "excluded_candidates_observed_v2.jsonl", excluded)
        with (paths["v2"] / "evidence_manifest_v2.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "position", "source_type", "trust_level", "published_at",
                             "content_hash", "url"])
            for row in rows:
                for pos, ref in enumerate(row["evidence_refs"], 1):
                    writer.writerow([row["sample_id"], pos, ref["source_type"], ref["trust_level"],
                                     ref["published_at"] or "", ref["content_hash"], ref["url"]])
        splits = {"dataset_version": DATASET_VERSION_V2, "rule": "topic-level; holdout fixed before training",
                  "holdout_topics": list(holdout_topics), "topic_split": dict(sorted(split_of.items())),
                  "sample_split": {row["sample_id"]: row["split"] for row in rows}}
        (paths["v2"] / "splits_v2.json").write_text(_dump(splits), encoding="utf-8")
        report["output_sha256"] = {name: _sha_file(paths["v2"] / name) for name in (
            "dataset_b_v2.jsonl", "dataset_b_v2_uncertain.jsonl", "splits_v2.json")}
    report["input_sha256"] = {e["name"]: e["sha256"] for e in manifest["run_files"]}
    report["input_sha256"][LABELS_FILE] = _sha_file(paths["labels"]) if paths["labels"].is_file() else None
    report["input_sha256"].update(manifest["v1_inputs"])
    (paths["v2"] / "validation_report_v2.json").write_text(_dump(report), encoding="utf-8")
    return report


def _resolve(gid: str, record: dict[str, Any], expert: dict[str, dict[str, Any]],
             problems: list[str]) -> dict[str, Any] | None:
    """Метка группы: экспертная; иначе silver v1 (кроме holdout); иначе — проблема «нет метки»."""
    if gid in expert:
        entry = expert[gid]
        return {"code": entry["code"], "confidence": float(entry["confidence"]), "rationale": entry["rationale"],
                "annotation_method": EXPERT_METHOD, "annotators": entry["annotators"], "review_status": "reviewed"}
    if record["holdout"]:
        problems.append(f"{gid}: группа holdout-темы «{record['topic']}» без экспертной метки")
        return None
    if record["silver"] is not None:
        silver = record["silver"]
        return {"code": silver["code"], "confidence": float(silver["confidence"]), "rationale": silver["rationale"],
                "annotation_method": SILVER_METHOD, "annotators": [ANNOTATOR], "review_status": "pending_human_review"}
    reason = f"silver-метки v1 расходятся {record['silver_conflict']}" if record["silver_conflict"] else "новая группа"
    problems.append(f"{gid}: нет экспертной метки ({reason}, тема «{record['topic']}»)")
    return None


def _finish(row: dict[str, Any], label: dict[str, Any]) -> dict[str, Any]:
    expert = label["annotation_method"] == EXPERT_METHOD
    row.update({"dataset_version": DATASET_VERSION_V2, "annotation_method": label["annotation_method"],
                "annotator": "+".join(label["annotators"]), "model_version": "human" if expert else ANNOTATOR,
                "review_status": label["review_status"]})
    return row


def _report_v2(rows, supervised, uncertain, problems, files, excluded, sources, holdout_topics) -> dict[str, Any]:  # noqa: ANN001, PLR0913
    by_split: dict[str, Counter] = defaultdict(Counter)
    for row in supervised:
        by_split[row["split"]][row["label"]] += 1
    holdout_by_topic: dict[str, Counter] = defaultdict(Counter)
    for row in supervised:
        if row["split"] == "holdout":
            holdout_by_topic[row["topic"]][row["label"]] += 1
    return {
        "dataset_version": DATASET_VERSION_V2,
        "status": "ok" if not problems else "problems",
        "problems": problems,
        "run_files": files,
        "rows_total": len(rows),
        "supervised": len(supervised),
        "uncertain": len(uncertain),
        "labels": {str(k): v for k, v in Counter(r["label"] for r in supervised).items()},
        "label_kinds": dict(Counter(r["label_kind"] for r in rows)),
        "label_sources": dict(sources),
        "reviewed_by_label": {str(k): v for k, v in Counter(
            r["label"] for r in supervised if r["review_status"] == "reviewed").items()},
        "topics": dict(Counter(r["topic"] for r in rows)),
        "groups": len({r["group_id"] for r in rows}),
        "holdout_topics": list(holdout_topics),
        "holdout_by_topic": {t: {str(k): v for k, v in c.items()} for t, c in sorted(holdout_by_topic.items())},
        "by_split": {k: {str(label): c for label, c in v.items()} for k, v in sorted(by_split.items())},
        "excluded_candidates_observed": len(excluded),
    }


# ---------------------------------------------------------------- быстрый замер точности выдачи


def export_runs(run_paths: list[Path], out_csv: Path, *, force: bool = False) -> dict[str, Any]:
    """Слепой лист по выдаче нескольких прогонов: общая карточка размечается один раз."""
    out_csv = Path(out_csv)
    manifest_path = out_csv.with_suffix(".manifest.json")
    if not force and (out_csv.exists() or manifest_path.exists()):
        raise DatasetV2Error(f"{out_csv} уже есть: повторная выгрузка затёрла бы разметку")
    items: list[dict[str, Any]] = []
    runs = []
    for path in run_paths:
        found, _ = parse_analytics(Path(path))
        if not found:
            raise DatasetV2Error(f"в {path} нет карточек выдачи")
        items.extend(asdict(item) for item in found)
        runs.append({"file": str(path), "run_id": found[0].run_id, "sha256": _sha_file(Path(path)),
                     "items": len(found)})
    if len({r["run_id"] for r in runs}) != len(runs):
        raise DatasetV2Error("прогоны должны быть разными")
    groups = _groups(items)
    rows, records = [], {}
    for gid, members in groups.items():
        rep = representative(members)
        evidence, urls = _evidence_text(rep["evidence"])
        rows.append({"group_id": gid, "topic": rep["topic"], "title": rep["title_ru"] or rep["title_auto"],
                     "cluster_title": rep["title_auto"], "evidence": evidence, "urls": urls, "label": "",
                     "comment": ""})
        records[gid] = {"topic": rep["topic"], "runs": {m["run_id"]: {"rank": m["rank"],
                                                                      "score": m["observed_runtime"].get("score")}
                                                        for m in members}}
    rows.sort(key=lambda row: _order_key(row["group_id"]))
    _write_sheet(out_csv, ["group_id", "topic", "title", "cluster_title", "evidence", "urls", "label", "comment"],
                 rows)
    manifest_path.write_text(_dump({"format": "ws-runs-compare-1", "runs": runs,
                                    "groups": dict(sorted(records.items()))}), encoding="utf-8")
    return {"sheet": str(out_csv), "manifest": str(manifest_path), "groups_to_label": len(rows),
            "runs": [{k: r[k] for k in ("run_id", "items")} for r in runs]}


def _share(codes: list[str]) -> dict[str, Any]:
    labeled = [c for c in codes if c != "U"]
    positives = sum(c == "R" for c in labeled)
    low, high = wilson(positives, len(labeled))
    return {"labeled": len(labeled), "R": positives, "U": len(codes) - len(labeled),
            "precision": None if not labeled else round(positives / len(labeled), 3),
            "ci95": [None if low is None else round(low, 3), None if high is None else round(high, 3)]}


def precision(sheet: Path, manifest: Path | None = None) -> dict[str, Any]:
    """Точность размеченной выдачи: доля R среди размеченных строк без U (пустые и U не считаются)."""
    rows = read_sheet(sheet)
    problems: list[str] = []
    if manifest is not None:
        meta = json.loads(Path(manifest).read_text(encoding="utf-8"))
        codes: dict[str, str] = {}
        for row in rows:
            code, _ = normalize_code(row.get("label", ""))
            if code == "?":
                problems.append(f"{row.get('group_id')}: неверный код «{row.get('label')}»")
            elif code:
                codes[row.get("group_id", "")] = code
        run_ids = [r["run_id"] for r in meta["runs"]]
        result: dict[str, Any] = {"unlabeled": sum(1 for g in meta["groups"] if g not in codes), "by_run": {},
                                  "problems": problems}
        for run_id in run_ids:
            present = [g for g, rec in meta["groups"].items() if run_id in rec["runs"]]
            result["by_run"][run_id] = {"groups": len(present), **_share([codes[g] for g in present if g in codes])}
        if len(run_ids) == 2:
            a, b = run_ids
            for name, keep in ((f"в обоих ({a} и {b})", lambda r: a in r and b in r),
                               (f"только в {a}", lambda r: a in r and b not in r),
                               (f"только в {b}", lambda r: b in r and a not in r)):
                present = [g for g, rec in meta["groups"].items() if keep(rec["runs"])]
                result["by_run"][name] = {"groups": len(present),
                                          **_share([codes[g] for g in present if g in codes])}
        return result
    by_topic: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for row in rows:
        code, _ = normalize_code(row.get("label", ""))
        if code == "?":
            problems.append(f"{row.get('topic')} #{row.get('rank')}: неверный код «{row.get('label')}»")
        elif code:
            rank = int(float(row.get("rank") or 0))
            by_topic[row.get("topic", "")].append((rank, code))
    topics = {topic: {**_share([c for _, c in items]),
                      "p_at_3": _share([c for r, c in items if 1 <= r <= 3])["precision"]}
              for topic, items in sorted(by_topic.items())}
    everything = [c for items in by_topic.values() for _, c in items]
    return {"rows": len(rows), "overall": _share(everything), "by_topic": topics, "problems": problems}


# ---------------------------------------------------------------- командная строка


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m ml.dataset_b.v2", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-root", default=".", help="каталог с analytics-*.txt и own-topics-*.json")
    parser.add_argument("--b-dir", default="ml/data/dataset_b", help="каталог датасета B (v1 и подкаталог v2)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("topics-check", help="проверка файлов новых тем")
    exp = sub.add_parser("export", help="фиксация состава v2 и слепые листы expert1.csv, expert2.csv")
    exp.add_argument("--force", action="store_true", help="перезаписать уже выгруженные листы")
    agr = sub.add_parser("agreement", help="каппа Коэна и лист арбитража")
    agr.add_argument("--force", action="store_true", help="перезаписать заполненный лист арбитража")
    sub.add_parser("merge", help="итоговые экспертные метки labels_v2.jsonl")
    sub.add_parser("build", help="сборка датасета B v2")
    runs = sub.add_parser("export-runs", help="слепой лист выдачи нескольких прогонов для замера точности")
    runs.add_argument("--out", required=True, help="путь к CSV; рядом пишется .manifest.json")
    runs.add_argument("--force", action="store_true")
    runs.add_argument("runs", nargs="+", help="файлы analytics-*.txt")
    prec = sub.add_parser("precision", help="точность размеченной выдачи")
    prec.add_argument("--manifest", help="manifest.json от export-runs (сравнение прогонов)")
    prec.add_argument("sheet")
    args = parser.parse_args(argv)
    b_dir, root = Path(args.b_dir), Path(args.project_root)
    try:
        if args.command == "topics-check":
            result = topics_check(b_dir)
        elif args.command == "export":
            result = export_sheets(root, b_dir, force=args.force)
            result["status"] = "ok"
        elif args.command == "agreement":
            result = agreement(b_dir, force=args.force)
        elif args.command == "merge":
            result = merge(b_dir)
        elif args.command == "build":
            result = build_v2(root, b_dir)
        elif args.command == "export-runs":
            result = export_runs([Path(p) for p in args.runs], Path(args.out), force=args.force)
            result["status"] = "ok"
        else:
            result = precision(Path(args.sheet), Path(args.manifest) if args.manifest else None)
            result["status"] = "ok" if not result["problems"] else "problems"
    except (DatasetV2Error, FileNotFoundError) as error:
        print(_dump({"status": "error", "error": str(error)}))
        return 2
    print(_dump(result))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
