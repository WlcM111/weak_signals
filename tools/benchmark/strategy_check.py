"""Быстрый замер (этапы 4.3–4.6): настоящий сбор collector по темам кейса и сопоставление с экспертами.

Этап 4.6 — по аудиту 11.10.2026:
- P01: часть названия в скобках («KV-cache») и аббревиатура больше не достаточны сами по себе — они засчитываются
  только вместе со словом контекста сигнала (значимые слова его синонимов). Синонимы и полное название — как раньше.
- P02: идентичность публикаций — ws_common.identity: arXiv abs/pdf/html/ar5iv/версии = одна работа, DOI,
  только известные параметры отслеживания удаляются.
- P03/P25: сохраняются документы сбора (strategy_check_documents.jsonl), доказательства совпадений
  (strategy_check_evidence.csv) и manifest (хэши входов, настройки без секретов, as_of, расширения, технологии,
  запуски адаптеров); режим --replay пересчитывает метрику офлайн, без сети и LLM.
- P04: адаптер со статусом COMPLETED и кодом остановки отображается как частичный.
- P25: незавершившийся за время ожидания сбор — ошибка измерения, а не полная коллекция; --rotate меняет порядок тем;
  --cutoff исключает документы, опубликованные после даты среза.
- P27: для каждого сигнала — есть ли эталонная ссылка с датой внутри окна поиска (temporal eligibility).
Запуск (в контейнере collector): python /tmp/strategy_check.py [--rotate N] [--cutoff ГГГГ-ММ-ДД] [--replay КАТАЛОГ]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import time
import unicodedata
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from ws_common.identity import identity_key, identity_keys

DOMAINS = (("индустриальн", "industrial_ai"), ("робототехник", "robotics"), ("инфраструктур", "ai_infrastructure"),
           ("финтех", "fintech"), ("безопасност", "ai_security"), ("edge", "edge"))
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_ACRONYM = re.compile(r"^[0-9A-Za-zА-Яа-яЁё]{2,5}$")
GENERIC_CONTEXT = frozenset("""ai ии artificial intelligence machine learning model models system systems data based using new
novel platform platforms solution solutions technology technologies approach method methods long short high low large
small open via for and the with of in on to для как на при или and или""".split())
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DSN", "CREDENTIAL")
WAIT_POLLS, WAIT_SECONDS = 240, 5


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return " ".join(_NON_WORD.sub(" ", text).replace("_", " ").split())


def _base(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def expert_rules(row: dict, synonyms: list[str]) -> dict:
    """Правила сигнала: достаточные термины (название, синонимы), фрагменты в скобках и аббревиатуры (только с
    контекстом), слова контекста — значимые слова синонимов (P01)."""
    parts = [p.strip() for g in re.findall(r"\(([^)]*)\)", row["title"]) for p in g.split(",") if p.strip()]
    sufficient = list(dict.fromkeys(t for t in (normalize(x) for x in [row["title"], *synonyms]) if len(t) >= 5))
    acronyms = list(dict.fromkeys(p.upper() for p in [*parts, *synonyms] if _ACRONYM.match(p.strip())
                                  and sum(c.isalpha() for c in p) >= 2 and p.strip().upper() == p.strip()))
    aliases = [a for a in dict.fromkeys(normalize(p) for p in parts) if len(a) >= 3 and a not in sufficient
               and a.upper() not in acronyms]
    context = {_base(w) for s in synonyms for w in normalize(s).split() if len(w) >= 4 and w not in GENERIC_CONTEXT}
    return {"sufficient": sufficient, "aliases": aliases, "acronyms": acronyms, "context": context}


def match_document(title: str, text: str, rules: dict) -> tuple[str, str] | None:
    """Правило и фрагмент совпадения документа с сигналом или None."""
    padded = f" {normalize(f'{title} {text}')} "
    for term in rules["sufficient"]:
        if f" {term} " in padded:
            return "synonym", term
    words = {_base(w) for w in padded.split()}

    def has_context(fragment: str) -> bool:
        own = {_base(w) for w in normalize(fragment).split()}
        return bool((rules["context"] - own) & words)

    for alias in rules["aliases"]:
        if f" {alias} " in padded and has_context(alias):
            return "alias+context", alias
    raw = f"{title} {text}"
    for acronym in rules["acronyms"]:
        if re.search(rf"(?<![\w]){re.escape(acronym)}(?![\w])", raw) and has_context(acronym):
            return "acronym+context", acronym
    return None


_URL_DATES = (re.compile(r"/(20\d\d)/(\d\d)/(\d\d)(?:/|$)"), re.compile(r"-(20\d\d)-(\d\d)-(\d\d)/?$"),
              re.compile(r"/(20\d\d)/(\d\d)(?:/|$)"))


def url_date(url: str) -> date | None:
    for pattern in _URL_DATES:
        found = pattern.search(url)
        if found:
            values = [int(v) for v in found.groups()] + [1] * (3 - len(found.groups()))
            try:
                return date(values[0], values[1], values[2])
            except ValueError:
                return None
    return None


def temporal_eligible(urls: list[str], window_start: date) -> bool | None:
    """True — есть ссылка с датой в окне; False — все датированные ссылки раньше окна; None — дат нет."""
    dates = [d for d in (url_date(u) for u in urls) if d]
    if not dates:
        return None
    return any(d >= window_start for d in dates)


def collection_limits() -> dict[str, int]:
    """Этап 4.7: время и лимит документов сбора — из окружения, как у orchestrator, а не заданы в замере."""
    return {"time_budget_seconds": int(os.environ.get("WS_COLLECT_TIME_BUDGET_SECONDS", "480")),
            "max_total_documents": int(os.environ.get("WS_COLLECT_MAX_TOTAL_DOCUMENTS", "1450"))}


def domain_of(topic: str) -> str:
    return next((tag for key, tag in DOMAINS if key in topic.lower()), "other")


def sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def settings_snapshot() -> dict:
    return {k: v for k, v in sorted(os.environ.items())
            if k.startswith("WS_") and not any(marker in k.upper() for marker in SECRET_MARKERS)}


def evaluate(topics: list[dict], experts: list[dict], synonyms: dict, cutoff: date | None, as_of: date) -> dict:
    """Сопоставление документов тем с экспертами: строки CSV, доказательства, итоги."""
    window_start = as_of - timedelta(days=730)
    rows, evidence, by_source = [], [], {}
    found_total = url_total = experts_total = eligible_total = 0
    for topic in topics:
        if topic.get("measurement_error"):
            continue
        docs = [d for d in topic["documents"]
                if not (cutoff and d.get("published_at") and date.fromisoformat(d["published_at"][:10]) > cutoff)]
        keyed = [(d, identity_keys(d["url"], d.get("doi"))) for d in docs]
        tag = domain_of(topic["topic"])
        found = 0
        for row in (r for r in experts if r["domain_tag"] == tag):
            rules = expert_rules(row, synonyms.get(row["row_id"]) or [])
            wanted = set().union(*(identity_keys(u) for u in row["source_urls"]))
            hits = []
            for doc, keys in keyed:
                if keys & wanted:
                    hits.append((doc, "organizer_url", identity_key(doc["url"], doc.get("doi"))))
                    continue
                decision = match_document(doc["title"], doc.get("text", ""), rules)
                if decision:
                    hits.append((doc, *decision))
            url_hit = any(rule == "organizer_url" for _, rule, _ in hits)
            sources = sorted({doc["source"] for doc, _, _ in hits})
            eligible = temporal_eligible(row["source_urls"], window_start)
            found += bool(hits)
            url_total += url_hit
            eligible_total += eligible is not False
            for source in sources:
                by_source[source] = by_source.get(source, 0) + 1
            for doc, rule, fragment in hits[:20]:
                evidence.append([row["row_id"], topic["topic"], rule, fragment, doc["url"], doc["source"],
                                 doc.get("published_at") or ""])
            rows.append([topic["topic"], row["row_id"], row["title"], len(hits), int(url_hit), ";".join(sources),
                         ";".join(sorted({rule for _, rule, _ in hits})),
                         {True: "в окне", False: "раньше окна", None: "даты нет"}[eligible]])
        count = sum(1 for r in experts if r["domain_tag"] == tag)
        topic["found"], topic["experts"] = found, count
        found_total, experts_total = found_total + found, experts_total + count
    return {"rows": rows, "evidence": evidence,
            "totals": {"experts": experts_total, "found": found_total, "organizer_url": url_total,
                       "by_source": dict(sorted(by_source.items())), "eligible_in_window": eligible_total,
                       "measurement_errors": [t["topic"] for t in topics if t.get("measurement_error")]}}


async def run_topic(index: int, topic: str, run_id: str) -> dict:
    import grpc
    import psycopg

    from collector.config import CollectorSettings
    from weaksignals.collector.v1 import collector_pb2, collector_pb2_grpc
    from weaksignals.common.v1 import common_pb2
    from weaksignals.insight.v1 import insight_pb2, insight_pb2_grpc

    settings = CollectorSettings()
    md = (("x-correlation-id", str(uuid.uuid4())), ("x-caller", "strategy-check"))
    terminal = {common_pb2.OPERATION_STATUS_COMPLETED, common_pb2.OPERATION_STATUS_PARTIAL,
                common_pb2.OPERATION_STATUS_FAILED, common_pb2.OPERATION_STATUS_CANCELLED}
    async with grpc.aio.insecure_channel(os.environ.get("WS_INSIGHT_ADDR", "insight:50053")) as channel:
        expansion = await insight_pb2_grpc.InsightServiceStub(channel).ExpandQuery(
            insight_pb2.ExpandQueryRequest(query_text=topic), timeout=180, metadata=md)
    address = os.environ.get("WS_COLLECTOR_SELF_ADDR", f"localhost:{settings.grpc_port}")
    async with grpc.aio.insecure_channel(address) as channel:
        stub = collector_pb2_grpc.CollectorServiceStub(channel)
        started = await stub.StartCollection(collector_pb2.StartCollectionRequest(
            idempotency_key=f"strategy-check:{run_id}:{index}", query_text=topic,
            terms=collector_pb2.SearchTerms(ru=list(expansion.ru_terms), en=list(expansion.en_terms),
                                            technologies=list(expansion.technologies)),
            mode=collector_pb2.COLLECTION_MODE_SEARCH,
            limits=collector_pb2.CollectionLimits(**collection_limits())),
            timeout=60, metadata=md)
        status = None
        for _ in range(WAIT_POLLS):
            status = await stub.GetCollection(collector_pb2.GetCollectionRequest(collection_id=started.collection_id),
                                              timeout=30, metadata=md)
            if status.status in terminal:
                break
            await asyncio.sleep(WAIT_SECONDS)
        if status is None or status.status not in terminal:
            return {"topic": topic, "collection_id": started.collection_id, "documents": [],
                    "measurement_error": "сбор не завершился за время ожидания"}
        documents, token = [], ""
        while True:
            last = False
            async for chunk in stub.StreamDocuments(collector_pb2.StreamDocumentsRequest(
                    collection_id=started.collection_id, chunk_size=200, page_token=token), timeout=120, metadata=md):
                for d in chunk.documents:
                    documents.append({
                        "url": d.url, "title": d.title, "text": d.text[:3000],
                        "source": common_pb2.SourceKey.Name(d.source_key).removeprefix("SOURCE_KEY_").lower(),
                        "published_at": d.published_at.ToDatetime().date().isoformat() if d.HasField("published_at") else None,
                        "doi": d.doi if d.HasField("doi") else None})
                token, last = chunk.next_page_token, chunk.last
            if last or not token:
                break
    with psycopg.connect(settings.pg_dsn) as conn:
        row = conn.execute("SELECT terms_technologies, technologies_mined, technologies_emerging FROM collector.collections "
                           "WHERE collection_id = %s", (started.collection_id,)).fetchone()
    runs = {}
    for r in status.adapter_runs:
        state = common_pb2.OperationStatus.Name(r.status).removeprefix("OPERATION_STATUS_")
        runs[common_pb2.SourceKey.Name(r.source_key).removeprefix("SOURCE_KEY_").lower()] = {
            "status": state, "documents": r.documents_found, "error_code": r.error_code,
            "partial": bool(r.error_code) and state == "COMPLETED"}
    return {"topic": topic, "collection_id": started.collection_id, "documents": documents,
            "expansion": {"ru": list(expansion.ru_terms), "en": list(expansion.en_terms),
                          "technologies": list(expansion.technologies)},
            "model": list(row[0] or []), "mined": list(row[1] or []), "emerging": list(row[2] or []), "runs": runs}


def write_outputs(out: Path, prefix: str, result: dict, topics: list[dict], manifest: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    report = {"totals": result["totals"], "topics": [{k: v for k, v in t.items() if k != "documents"} | {
        "documents": len(t.get("documents", []))} for t in topics]}
    (out / f"{prefix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(out / f"{prefix}.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["topic", "row_id", "title", "documents_matched", "organizer_url", "sources", "rules", "eligible"])
        writer.writerows(result["rows"])
    with open(out / f"{prefix}_evidence.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["row_id", "topic", "rule", "fragment", "url", "source", "published_at"])
        writer.writerows(result["evidence"])
    (out / f"{prefix}_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Быстрый замер стратегии сбора")
    parser.add_argument("--topics", default="/tmp/case_topics.txt")
    parser.add_argument("--dataset", default="/tmp/dataset.jsonl")
    parser.add_argument("--synonyms", default="/tmp/synonyms.yaml")
    parser.add_argument("--out", default="/tmp")
    parser.add_argument("--rotate", type=int, default=0)
    parser.add_argument("--cutoff", type=date.fromisoformat, default=None)
    parser.add_argument("--replay", default=None, help="каталог прошлого замера: пересчёт без сети")
    args = parser.parse_args()
    import yaml

    experts = [r for r in (json.loads(l) for l in open(args.dataset, encoding="utf-8") if l.strip())
               if int(r.get("label") or 0) == 1]
    synonyms = yaml.safe_load(open(args.synonyms, encoding="utf-8"))["synonyms"]
    started = time.monotonic()
    if args.replay:
        source = Path(args.replay)
        manifest = json.loads((source / "strategy_check_manifest.json").read_text(encoding="utf-8"))
        by_topic: dict[str, list[dict]] = {}
        for line in open(source / "strategy_check_documents.jsonl", encoding="utf-8"):
            record = json.loads(line)
            by_topic.setdefault(record.pop("topic"), []).append(record)
        topics = [{"topic": t, "documents": by_topic.get(t, [])} for t in manifest["topic_order"]]
        as_of = date.fromisoformat(manifest["as_of"][:10])
        prefix = "strategy_check_replay"
    else:
        names = [l.strip() for l in open(args.topics, encoding="utf-8") if l.strip()]
        shift = args.rotate % len(names) if names else 0
        names = names[shift:] + names[:shift]
        run_id, as_of = time.strftime("%Y%m%d-%H%M%S"), datetime.now(UTC).date()
        topics = []
        for index, name in enumerate(names):
            topic = asyncio.run(run_topic(index, name, run_id))
            topics.append(topic)
            partial = {k: v["error_code"] for k, v in topic.get("runs", {}).items() if v["error_code"]}
            print(f"{name[:48]:48} | документов {len(topic['documents'])}"
                  + (f" | ОШИБКА ИЗМЕРЕНИЯ: {topic['measurement_error']}" if topic.get("measurement_error") else "")
                  + (f" | неполные источники: {partial}" if partial else ""), flush=True)
            print(f"    из корпусов ({len(topic.get('emerging', []))}): {topic.get('emerging', [])[:10]}", flush=True)
            print(f"    извлечённые ({len(topic.get('mined', []))}): {topic.get('mined', [])[:8]}", flush=True)
        manifest = {
            "as_of": datetime.now(UTC).isoformat(), "cutoff": args.cutoff.isoformat() if args.cutoff else None,
            "topic_order": names, "rotate": args.rotate, "limits": collection_limits(),
            "inputs": {"tool": sha256(__file__), "dataset": sha256(args.dataset), "synonyms": sha256(args.synonyms)},
            "settings": settings_snapshot(),
            "topics": [{k: v for k, v in t.items() if k != "documents"} for t in topics],
        }
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "strategy_check_documents.jsonl", "w", encoding="utf-8") as handle:
            for topic in topics:
                for doc in topic["documents"]:
                    handle.write(json.dumps({"topic": topic["topic"], **doc}, ensure_ascii=False) + "\n")
        prefix = "strategy_check"
    result = evaluate(topics, experts, synonyms, args.cutoff, as_of)
    for topic in topics:
        if not topic.get("measurement_error"):
            print(f"{topic['topic'][:48]:48} | эксперты {topic['found']}/{topic['experts']}", flush=True)
    write_outputs(Path(args.out), prefix, result, topics, manifest)
    totals = result["totals"]
    print(f"\nИТОГО за {time.monotonic() - started:.0f} с: экспертных сигналов найдено {totals['found']}/{totals['experts']}; "
          f"ссылок организаторов: {totals['organizer_url']}; по источникам: {totals['by_source']}; "
          f"сигналов со ссылкой в окне поиска или без даты: {totals['eligible_in_window']}"
          + (f"; ОШИБКИ ИЗМЕРЕНИЯ: {totals['measurement_errors']}" if totals["measurement_errors"] else ""), flush=True)


if __name__ == "__main__":
    main()
