"""Разметка внешними моделями для режима А (решение владельца проекта от 04.10.2026).

Синонимы экспертных сигналов, коды карточек выдачи и решения по спорным парам «сигнал — объект» делают две
независимые модели в новых чатах без контекста проекта (ChatGPT 6 Astra Max и Claude Fable 5.1 Max). Модуль
объединяет их ответы в форматы инструментов оценки:
- синонимы → YAML протокола match-v1;
- коды карточек → листы `expert1.csv` и `expert2.csv` для `tools/quality/human_eval.py`;
- решения по парам → листы `reviewer_1.csv` и `reviewer_2.csv` для `match-score`.

Ответы проверяются строго: тот же набор строк, допустимые коды, без пропусков. Иначе — ошибка, а не тихое
частичное заполнение.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

from ml.benchmark.expert_match import MIN_TERM_CHARS, SHEET_FIELDS, normalize, review_pairs
from ml.benchmark.snapshot import Snapshot

CARD_FIELDS = ["card_id", "topic", "title", "description", "advantage", "case_example", "sources", "label", "comment"]
CARD_CODES = {"R", "N-OFF", "N-GEN", "N-OVR", "N-MAT", "N-NOI", "N-HYP", "N-FUND", "U"}
PAIR_DECISIONS = {"Y", "N", "U"}
PAIR_EXPORT_FIELDS = ["pair_id", "topic", "expert_title", "expert_description", "object_kind", "object_title",
                      "object_text", "matched_term"]


def read_csv(path: Path) -> list[dict[str, str]]:
    """CSV в UTF-8 (с BOM или без)."""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    """CSV в UTF-8 с BOM — открывается в Excel и читается инструментами оценки."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_synonyms(path: Path) -> dict[str, list[str]]:
    """Ответ модели `synonyms.jsonl`: строка JSON на сигнал."""
    result: dict[str, list[str]] = {}
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        row_id = str(item["row_id"])
        if row_id in result:
            raise ValueError(f"{path}: строка {number}: row_id {row_id} повторяется")
        if not isinstance(item.get("synonyms"), list):
            raise ValueError(f"{path}: строка {number}: synonyms — не список")
        result[row_id] = [str(term).strip() for term in item["synonyms"] if str(term).strip()]
    return result


def merge_synonyms(answers: list[dict[str, list[str]]], experts: list[dict[str, Any]]) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Объединение синонимов моделей по правилам протокола: без повторов, без коротких и без самого названия."""
    ids = [str(row["row_id"]) for row in experts]
    for number, answer in enumerate(answers, 1):
        if set(answer) != set(ids):
            raise ValueError(f"ответ {number}: набор row_id не совпадает с экспертным списком "
                             f"(лишние {sorted(set(answer) - set(ids))[:5]}, нет {sorted(set(ids) - set(answer))[:5]})")
    titles = {str(row["row_id"]): normalize(str(row.get("title") or "")) for row in experts}
    merged: dict[str, list[str]] = {}
    dropped = {"повтор": 0, "короче порога": 0, "равен названию": 0}
    for row_id in ids:
        seen: set[str] = set()
        kept: list[str] = []
        for answer in answers:
            for term in answer[row_id]:
                norm = normalize(term)
                if len(norm) < MIN_TERM_CHARS:
                    dropped["короче порога"] += 1
                elif norm == titles[row_id]:
                    dropped["равен названию"] += 1
                elif norm in seen:
                    dropped["повтор"] += 1
                else:
                    seen.add(norm)
                    kept.append(term)
        merged[row_id] = kept
    return merged, dropped


def synonyms_yaml(merged: dict[str, list[str]], provenance: list[str]) -> str:
    """YAML файла синонимов; строки — в JSON-кавычках (это допустимые скаляры YAML)."""
    lines = ["# Синонимы экспертных сигналов для протокола match-v1 (docs/benchmark/MATCHING_PROTOCOL.md).",
             "# Файл зафиксирован до оценки прогонов и по её результатам не меняется; SHA-256 пишется в matches.json."]
    lines += [f"# {item}" for item in provenance]
    lines.append("synonyms:")
    for row_id, terms in merged.items():
        if terms:
            lines.append(f"  {row_id}:")
            lines += [f"    - {json.dumps(term, ensure_ascii=False)}" for term in terms]
        else:
            lines.append(f"  {row_id}: []")
    return "\n".join(lines) + "\n"


def cards_sheets(cards_csv: Path, label_files: list[Path], out: Path) -> dict[str, Any]:
    """Листы `expert1.csv`, `expert2.csv` для human_eval из карточек и кодов двух моделей."""
    cards = read_csv(cards_csv)
    by_id = {row["card_id"]: row for row in cards}
    if len(by_id) != len(cards):
        raise ValueError(f"{cards_csv}: card_id повторяются")
    summary: dict[str, Any] = {"cards": len(cards), "sheets": []}
    for number, path in enumerate(label_files, 1):
        labels = read_csv(path)
        ids = [row["card_id"].strip() for row in labels]
        if sorted(ids) != sorted(by_id):
            raise ValueError(f"{path}: набор card_id не совпадает с {cards_csv} "
                             f"(лишние {sorted(set(ids) - set(by_id))[:5]}, нет {sorted(set(by_id) - set(ids))[:5]})")
        bad = sorted({row["label"].strip() for row in labels} - CARD_CODES)
        if bad:
            raise ValueError(f"{path}: недопустимые коды {bad}")
        decided = {row["card_id"].strip(): row for row in labels}
        rows = [dict(card, label=decided[card["card_id"]]["label"].strip(),
                     comment=decided[card["card_id"]].get("comment", "").strip()) for card in cards]
        write_csv(Path(out) / f"expert{number}.csv", CARD_FIELDS, rows)
        summary["sheets"].append({"sheet": f"expert{number}.csv", "source": Path(path).name, "sha256": sha256_file(path)})
    (Path(out) / "sheets_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def _object_texts(snapshot: Snapshot) -> dict[str, str]:
    """Текст объектов снимка для проверяющего: документ, событие сбора, кандидат, карточка."""
    texts: dict[str, str] = {}
    for row in snapshot.jsonl("documents.jsonl"):
        texts[str(row["document_id"])] = " ".join(str(row.get("body_text") or "").split())[:600]
    for event in snapshot.jsonl("document_events.jsonl"):
        texts[f"event:{event['run_id']}:{event['seq']}"] = str(event.get("url") or "")
    if snapshot.has("analysis.json"):
        for row in snapshot.json("analysis.json").get("candidates") or []:
            texts[str(row["candidate_id"])] = "ключевые фразы: " + ", ".join(row.get("keyphrases") or [])
    if snapshot.has("job.json"):
        for item in snapshot.json("job.json").get("items") or []:
            texts[str(item["candidate_id"])] = " ".join(str(item.get("description_ru") or "").split())[:600]
    return texts


def pairs_export(match_dir: Path, snapshots: list[Snapshot], out_csv: Path) -> int:
    """Спорные пары в виде для проверяющей модели: сигнал, объект, его текст и совпавший термин."""
    result = json.loads((Path(match_dir) / "matches.json").read_text(encoding="utf-8"))
    texts: dict[str, dict[str, str]] = {snapshot.name: _object_texts(snapshot) for snapshot in snapshots}
    rows = []
    for pair in review_pairs(result):
        rows.append({"pair_id": pair["pair_id"], "topic": pair["topic"], "expert_title": pair["expert_title"],
                     "expert_description": pair["expert_description"], "object_kind": pair["object_kind"],
                     "object_title": pair["object_title"],
                     "object_text": texts.get(pair["run"], {}).get(pair["object_id"], ""),
                     "matched_term": pair["matched_term"]})
    write_csv(out_csv, PAIR_EXPORT_FIELDS, rows)
    return len(rows)


def pairs_import(match_dir: Path, answer_files: list[Path]) -> dict[str, Any]:
    """Решения моделей по парам → `reviewer_1.csv`, `reviewer_2.csv` (решения независимы, листы не смешиваются)."""
    folder = Path(match_dir)
    summary: dict[str, Any] = {"sheets": []}
    for number, path in enumerate(answer_files, 1):
        sheet = folder / f"reviewer_{number}.csv"
        rows = read_csv(sheet)
        answers = {row["pair_id"].strip(): row for row in read_csv(path)}
        ids = {row["pair_id"] for row in rows}
        if set(answers) != ids:
            raise ValueError(f"{path}: набор pair_id не совпадает с {sheet.name} "
                             f"(лишние {sorted(set(answers) - ids)[:5]}, нет {sorted(ids - set(answers))[:5]})")
        bad = sorted({row["decision"].strip().upper() for row in answers.values()} - PAIR_DECISIONS)
        if bad:
            raise ValueError(f"{path}: недопустимые решения {bad}")
        for row in rows:
            answer = answers[row["pair_id"]]
            row["decision"] = answer["decision"].strip().upper()
            row["comment"] = answer.get("comment", "").strip()
        write_csv(sheet, SHEET_FIELDS, rows)
        summary["sheets"].append({"sheet": sheet.name, "source": Path(path).name, "sha256": sha256_file(path)})
    (folder / "reviewers_manifest.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary
