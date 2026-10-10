"""Сопоставление выдачи с экспертным списком по протоколу match-v1 (аудит A4, этап 2).

Протокол — docs/benchmark/MATCHING_PROTOCOL.md. Кратко: экспертный сигнал совпадает с объектом этапа (документом,
кандидатом, карточкой) по ссылке (URL, DOI, arXiv ID из `source_urls`) — автоматически — или по термину (название
сигнала, термин в скобках, синоним из утверждённого до прогона файла) — это спорное совпадение, его решают два
человека независимо. Знаменатель — экспертные сигналы темы. Неразмеченные спорные совпадения не засчитываются.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from ml.benchmark.snapshot import Snapshot
from ml.benchmark.stats import bootstrap_ci, cohen_kappa, kappa_ci, mean
from ml.hit_rate import DOMAIN_QUERY

PROTOCOL = "match-v1"
STAGES = ("found", "collected", "relevant", "clustered", "pool", "judged", "finalized", "shown")
STAGE_RU = {"found": "найден источником", "collected": "в коллекции", "relevant": "прошёл близость к запросу",
            "clustered": "в кандидате analyzer", "pool": "в пуле рубрики", "judged": "признан рубрикой",
            "finalized": "карточка доведена", "shown": "в выдаче"}
MIN_TERM_CHARS = 5
SHEET_FIELDS = ["pair_id", "run", "topic", "expert_row_id", "expert_title", "expert_description", "object_kind",
                "object_id", "object_title", "rule", "matched_term", "decision", "comment"]
TOPIC_DOMAIN = {query: domain for domain, query in DOMAIN_QUERY.items()}
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#]+", re.IGNORECASE)
_ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", re.IGNORECASE)
_ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", re.IGNORECASE)


def normalize(text: str) -> str:
    """Нижний регистр, «ё» → «е», всё кроме букв и цифр — пробел."""
    lowered = (text or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", lowered).replace("_", " ").split())


def url_keys(url: str | None, doi: str | None = None) -> set[str]:
    """Ключи точного совпадения: адрес без схемы, www и параметров; DOI; идентификатор arXiv."""
    keys: set[str] = set()
    for value in (url or "", doi or ""):
        text = value.strip()
        if not text:
            continue
        for found in _ARXIV_DOI_RE.findall(text) + _ARXIV_RE.findall(text):
            keys.add(f"arxiv:{found.lower()}")
        for found in _DOI_RE.findall(text):
            keys.add(f"doi:{found.lower().rstrip('.,;)')}")
        if text.startswith(("http://", "https://")):
            rest = text.split("://", 1)[1].split("#", 1)[0].split("?", 1)[0].lower().removeprefix("www.").rstrip("/")
            keys.add(f"url:{rest}")
    return keys


_ACRONYM_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё]{2,4}$")


def expert_acronyms(row: dict[str, Any], synonyms: dict[str, list[str]]) -> list[str]:
    """Короткие названия сигнала (OCS, PBM, SMR, MMF, KYA, НПЗ): только как отдельное слово в верхнем регистре.

    Этап 4.3: порог MIN_TERM_CHARS отбрасывал их, и совпадения по 6 сигналам не засчитывались. Это исправление
    измерения, а не поиска; совпадение по аббревиатуре идёт на проверку парами, как и по синониму.
    """
    title = str(row.get("title") or "")
    parts = [p.strip() for inner in re.findall(r"\(([^)]+)\)", title) for p in inner.split(",")]
    parts += [str(s).strip() for s in synonyms.get(str(row["row_id"]), [])]
    found = []
    for part in parts:
        if _ACRONYM_RE.match(part) and sum(ch.isalpha() for ch in part) >= 2:
            acronym = part.upper().replace("Ё", "Е")
            if acronym not in found:
                found.append(acronym)
    return found


def acronym_hit(raw: str, acronyms: list[str]) -> str | None:
    """Первая аббревиатура, которая стоит в тексте отдельным словом в верхнем регистре."""
    for acronym in acronyms:
        if re.search(rf"(?<![\w]){re.escape(acronym)}(?![\w])", raw or ""):
            return acronym
    return None


def expert_terms(row: dict[str, Any], synonyms: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Термины сигнала: название, термины в скобках, синонимы из файла; короче MIN_TERM_CHARS не берутся."""
    title = str(row.get("title") or "")
    terms = [(title, "term")] + [(inner, "term") for inner in re.findall(r"\(([^)]+)\)", title)]
    terms += [(str(s), "synonym") for s in synonyms.get(str(row["row_id"]), [])]
    seen, result = set(), []
    for term, rule in terms:
        norm = normalize(term)
        if len(norm) >= MIN_TERM_CHARS and norm not in seen:
            seen.add(norm)
            result.append((norm, rule))
    return result


def load_synonyms(path: str | None) -> tuple[dict[str, list[str]], str]:
    """Синонимы `row_id → [термины]` из YAML и SHA-256 файла; без файла — пусто."""
    if not path:
        return {}, ""
    raw = Path(path).read_bytes()
    payload = yaml.safe_load(raw.decode("utf-8")) or {}
    return {str(k): [str(t) for t in (v or [])] for k, v in (payload.get("synonyms") or {}).items()}, hashlib.sha256(raw).hexdigest()


def _objects(snapshot: Snapshot) -> dict[str, list[dict[str, Any]]]:
    """Объекты каждого этапа: текст для терминов и ключи ссылок."""
    docs = {str(row["document_id"]): row for row in snapshot.jsonl("documents.jsonl")}

    def doc_object(doc_id: str) -> dict[str, Any]:
        row = docs[doc_id]
        return {"kind": "document", "id": doc_id, "title": row.get("title") or "",
                "raw": f"{row.get('title', '')} {row.get('body_text', '')}",
                "text": normalize(f"{row.get('title', '')} {row.get('body_text', '')}"),
                "keys": url_keys(row.get("url"), row.get("doi"))}

    stages: dict[str, list[dict[str, Any]]] = {}
    events = snapshot.jsonl("document_events.jsonl")
    found = [{"kind": "event", "id": f"event:{e['run_id']}:{e['seq']}", "title": e.get("title") or "",
              "raw": e.get("title") or "", "text": normalize(e.get("title") or ""), "keys": url_keys(e.get("url"), e.get("doi"))} for e in events]
    stages["found"] = found + [doc_object(d) for d in docs]
    stages["collected"] = [doc_object(d) for d in docs]
    capture = snapshot.json("capture.json") if snapshot.has("capture.json") else {}
    f = capture.get("funnel") or {}
    if f:
        dropped = set(f.get("near_dup_dropped") or []) | set(f.get("relevance_dropped") or [])
        stages["relevant"] = [doc_object(d) for d in docs if d not in dropped]
        selected = {d for cluster in f.get("clusters_selected") or [] for d in cluster}
        stages["clustered"] = [doc_object(d) for d in docs if d in selected]
    analysis = snapshot.json("analysis.json") if snapshot.has("analysis.json") else {}
    trace = (snapshot.json("trace.json") or {}).get("narrate") or {} if snapshot.has("trace.json") else {}
    judged = trace.get("judged") or {}

    def candidate_object(row: dict[str, Any]) -> dict[str, Any]:
        verdict = judged.get(str(row["candidate_id"])) or {}
        keys: set[str] = set()
        for doc_id in row.get("document_ids") or []:
            if doc_id in docs:
                keys |= url_keys(docs[doc_id].get("url"), docs[doc_id].get("doi"))
        text = " ".join([str(row.get("title_auto") or ""), " ".join(row.get("keyphrases") or []),
                         str(verdict.get("technology_ru") or ""), str(verdict.get("profile_ru") or "")])
        return {"kind": "candidate", "id": str(row["candidate_id"]), "title": row.get("title_auto") or "",
                "raw": text, "text": normalize(text), "keys": keys}

    candidates = {str(row["candidate_id"]): candidate_object(row) for row in analysis.get("candidates") or []}
    if trace:
        stages["pool"] = [candidates[c] for c in trace.get("pool") or [] if c in candidates]
        eligible = list(trace.get("accepted") or []) + list(trace.get("uncertain_fill") or [])
        stages["judged"] = [candidates[c] for c in eligible if c in candidates]
        dropped = set(trace.get("review_dropped") or {}) | set(trace.get("final_dropped") or {})
        finalized = {c for r in trace.get("rounds") or [] for c in r.get("finalized") or []} - dropped
        finalized |= {row[1] for row in trace.get("shown") or []}
        stages["finalized"] = [candidates[c] for c in finalized if c in candidates]
    job = snapshot.json("job.json") if snapshot.has("job.json") else {}
    sources: dict[str, list[str]] = {}
    for row in job.get("sources") or []:
        sources.setdefault(str(row["item_id"]), []).append(str(row["document_id"]))
    shown = []
    for item in job.get("items") or []:
        keys: set[str] = set()
        for doc_id in sources.get(str(item.get("item_id")), []):
            if doc_id in docs:
                keys |= url_keys(docs[doc_id].get("url"), docs[doc_id].get("doi"))
        shown.append({"kind": "card", "id": str(item["candidate_id"]), "title": item.get("title_ru") or "",
                      "raw": f"{item.get('title_ru', '')} {item.get('description_ru', '')}",
                      "text": normalize(f"{item.get('title_ru', '')} {item.get('description_ru', '')}"), "keys": keys})
    stages["shown"] = shown
    return stages


def match(snapshots: list[Snapshot], dataset_rows: list[dict[str, Any]], synonyms: dict[str, list[str]]) -> dict[str, Any]:
    """Совпадения экспертных сигналов с объектами каждого этапа каждого задания."""
    experts = [row for row in dataset_rows if int(row.get("label", 0)) == 1]
    runs = []
    for snapshot in snapshots:
        domain = TOPIC_DOMAIN.get(snapshot.topic)
        if domain is None:
            runs.append({"snapshot": snapshot.name, "topic": snapshot.topic, "domain": None,
                         "note": "тема не из списка кейса: экспертного списка для неё нет"})
            continue
        stages = _objects(snapshot)
        rows = []
        for expert in (e for e in experts if e.get("domain_tag") == domain):
            keys = set().union(*(url_keys(u) for u in expert.get("source_urls") or [])) if expert.get("source_urls") else set()
            terms = expert_terms(expert, synonyms)
            acronyms = expert_acronyms(expert, synonyms)
            found: dict[str, list[dict[str, str]]] = {}
            for stage in STAGES:
                if stage not in stages:
                    continue
                hits = []
                for obj in stages[stage]:
                    if keys & obj["keys"]:
                        hits.append({"object_kind": obj["kind"], "object_id": obj["id"], "object_title": obj["title"][:300],
                                     "rule": "link", "term": sorted(keys & obj["keys"])[0]})
                        continue
                    padded = f" {obj['text']} "
                    for term, rule in terms:
                        if f" {term} " in padded:
                            hits.append({"object_kind": obj["kind"], "object_id": obj["id"],
                                         "object_title": obj["title"][:300], "rule": rule, "term": term})
                            break
                    else:
                        acronym = acronym_hit(obj.get("raw", ""), acronyms)
                        if acronym:
                            hits.append({"object_kind": obj["kind"], "object_id": obj["id"],
                                         "object_title": obj["title"][:300], "rule": "acronym", "term": acronym})
                found[stage] = hits
            rows.append({"row_id": expert["row_id"], "title": expert.get("title"), "description": expert.get("description"),
                         "stages": found, "available_stages": [s for s in STAGES if s in stages]})
        runs.append({"snapshot": snapshot.name, "topic": snapshot.topic, "domain": domain, "experts": rows})
    return {"protocol": PROTOCOL, "runs": runs}


def review_pairs(result: dict[str, Any]) -> list[dict[str, str]]:
    """Спорные пары «сигнал — объект» (совпадение по термину или синониму) для двух проверяющих."""
    pairs: dict[str, dict[str, str]] = {}
    for run in result["runs"]:
        for expert in run.get("experts") or []:
            for hits in expert["stages"].values():
                for hit in hits:
                    if hit["rule"] == "link":
                        continue
                    pair_id = hashlib.sha256(f"{run['snapshot']}|{expert['row_id']}|{hit['object_id']}".encode()).hexdigest()[:16]
                    pairs.setdefault(pair_id, {
                        "pair_id": pair_id, "run": run["snapshot"], "topic": run["topic"], "expert_row_id": expert["row_id"],
                        "expert_title": str(expert.get("title") or ""), "expert_description": str(expert.get("description") or "")[:500],
                        "object_kind": hit["object_kind"], "object_id": hit["object_id"], "object_title": hit["object_title"],
                        "rule": hit["rule"], "matched_term": hit["term"], "decision": "", "comment": ""})
    return sorted(pairs.values(), key=lambda p: (p["run"], p["expert_row_id"], p["object_id"]))


def write_sheets(pairs: list[dict[str, str]], out: Path) -> None:
    """Два одинаковых листа для независимых проверяющих (Y — совпадает, N — нет, U — нельзя решить)."""
    out.mkdir(parents=True, exist_ok=True)
    for name in ("reviewer_1.csv", "reviewer_2.csv"):
        with (out / name).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SHEET_FIELDS)
            writer.writeheader()
            writer.writerows(pairs)


def _read_decisions(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["pair_id"]: (row.get("decision") or "").strip().upper() for row in csv.DictReader(handle) if row.get("pair_id")}


def score(folder: Path) -> dict[str, Any]:
    """Согласие проверяющих, подтверждённые совпадения, полнота по этапам и темам, первый этап потери."""
    result = json.loads((folder / "matches.json").read_text(encoding="utf-8"))
    one, two = _read_decisions(folder / "reviewer_1.csv"), _read_decisions(folder / "reviewer_2.csv")
    resolution = _read_decisions(folder / "resolution.csv")
    pairs = {p["pair_id"]: p for p in review_pairs(result)}
    both = [pid for pid in pairs if one.get(pid) in ("Y", "N") and two.get(pid) in ("Y", "N")]
    a = [1 if one[pid] == "Y" else 0 for pid in both]
    b = [1 if two[pid] == "Y" else 0 for pid in both]
    confirmed_pairs = {pid for pid in both if one[pid] == two[pid] == "Y"} | {pid for pid, d in resolution.items() if d == "Y"}
    rejected_pairs = {pid for pid in both if one[pid] == two[pid] == "N"} | {pid for pid, d in resolution.items() if d == "N"}
    disputed = [pairs[pid] | {"reviewer_1": one.get(pid, ""), "reviewer_2": two.get(pid, "")}
                for pid in pairs if pid not in confirmed_pairs and pid not in rejected_pairs]
    with (folder / "disagreements.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_FIELDS + ["reviewer_1", "reviewer_2"])
        writer.writeheader()
        writer.writerows(disputed)
    per_run, first_loss = [], {}
    for run in result["runs"]:
        experts = run.get("experts")
        if not experts:
            continue
        recall, losses = {}, {}
        for stage in STAGES:
            if stage not in experts[0]["available_stages"]:
                continue
            hit = 0
            for expert in experts:
                if any(h["rule"] == "link" or _pair(run, expert, h) in confirmed_pairs for h in expert["stages"].get(stage, [])):
                    hit += 1
            recall[stage] = {"hits": hit, "of": len(experts), "share": round(hit / len(experts), 4)}
        for expert in experts:
            previous = True
            loss = "не найден ни на одном этапе"
            for stage in STAGES:
                if stage not in expert["stages"]:
                    continue
                ok = any(h["rule"] == "link" or _pair(run, expert, h) in confirmed_pairs for h in expert["stages"][stage])
                if not ok:
                    loss = STAGE_RU[stage] if stage != "found" else loss
                    if stage == "found":
                        previous = False
                    break
            else:
                loss = "дошёл до выдачи"
            if previous or loss == "дошёл до выдачи":
                losses[expert["row_id"]] = loss
            else:
                losses[expert["row_id"]] = "не найден ни на одном этапе"
            first_loss[loss] = first_loss.get(loss, 0) + 1
        per_run.append({"snapshot": run["snapshot"], "topic": run["topic"], "domain": run["domain"], "recall": recall,
                        "first_loss": losses})
    overall = {}
    for stage in STAGES:
        shares = [r["recall"][stage]["share"] for r in per_run if stage in r["recall"]]
        if shares:
            overall[stage] = {"mean_over_topics": round(mean(shares), 4), "ci95_bootstrap_topics": bootstrap_ci(shares),
                              "topics": len(shares)}
    report = {"protocol": result["protocol"], "pairs": len(pairs), "labeled_by_both": len(both),
              "kappa": cohen_kappa(a, b), "kappa_ci95": kappa_ci(a, b), "confirmed_pairs": len(confirmed_pairs),
              "rejected_pairs": len(rejected_pairs), "unresolved_pairs": len(disputed), "recall_by_stage": overall,
              "first_loss": first_loss, "runs": per_run,
              "note": "Неразмеченные и спорные пары не засчитаны: полнота — нижняя граница до разрешения разногласий."}
    (folder / "match_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def _pair(run: dict[str, Any], expert: dict[str, Any], hit: dict[str, str]) -> str:
    return hashlib.sha256(f"{run['snapshot']}|{expert['row_id']}|{hit['object_id']}".encode()).hexdigest()[:16]


def synonyms_template(dataset_rows: list[dict[str, Any]]) -> str:
    """Шаблон файла синонимов: заполняется людьми до прогона и фиксируется (его SHA-256 попадает в отчёт)."""
    lines = ["# Синонимы экспертных сигналов для протокола match-v1. Заполняется ДО оценки прогона и не меняется",
             "# по её результатам; SHA-256 файла пишется в отчёт сопоставления.", "synonyms:"]
    for row in dataset_rows:
        if int(row.get("label", 0)) == 1:
            lines.append(f"  {row['row_id']}: []  # {str(row.get('title') or '').replace(chr(10), ' ')[:110]}")
    return "\n".join(lines) + "\n"
