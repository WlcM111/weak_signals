"""Быстрый замер (этап 4.3): настоящий сбор сервиса collector по шести темам кейса и сопоставление с экспертами.

Запускается внутри контейнера collector. Для каждой темы:
1. ExpandQuery в insight — фразы темы и технологии модели (как в задании);
2. StartCollection в этом же collector — тот же код, что в полном прогоне: широкий проход, архив новостей,
   технологии из корпусов (оценка возникновения), извлечённые, второй круг;
3. документы коллекции — StreamDocuments; выбранные технологии — из таблицы collections.
Экспертный список в запросы не попадает — только сопоставление после сбора: название технологии или синоним
в заголовке или тексте (match-v1), короткое название (OCS, PBM…) — отдельным словом в верхнем регистре
(исправление измерения, этап 4.3), либо ссылка организаторов. Итог: /tmp/strategy_check.json и .csv.
Аргументы: файл тем, датасет, синонимы (по умолчанию /tmp/case_topics.txt, /tmp/dataset.jsonl, /tmp/synonyms.yaml).
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import sys
import time
import unicodedata
import uuid

import grpc
import psycopg
import yaml

from collector.config import CollectorSettings
from weaksignals.collector.v1 import collector_pb2, collector_pb2_grpc
from weaksignals.common.v1 import common_pb2
from weaksignals.insight.v1 import insight_pb2, insight_pb2_grpc

DOMAINS = (("индустриальн", "industrial_ai"), ("робототехник", "robotics"), ("инфраструктур", "ai_infrastructure"),
           ("финтех", "fintech"), ("безопасност", "ai_security"), ("edge", "edge"))
TERMINAL = {common_pb2.OPERATION_STATUS_COMPLETED, common_pb2.OPERATION_STATUS_PARTIAL,
            common_pb2.OPERATION_STATUS_FAILED, common_pb2.OPERATION_STATUS_CANCELLED}
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_ACRONYM = re.compile(r"^[0-9A-Za-zА-Яа-яЁё]{2,4}$")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return " ".join(_NON_WORD.sub(" ", text).replace("_", " ").split())


def url_key(url: str) -> str:
    url = re.sub(r"^https?://(www\.)?", "", (url or "").strip().lower()).split("#")[0].split("?")[0].rstrip("/")
    arxiv = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url)
    return f"arxiv:{arxiv.group(1)}" if arxiv else url


def expert_terms(row: dict, synonyms: list[str]) -> tuple[list[str], list[str]]:
    parts = [p.strip() for g in re.findall(r"\(([^)]*)\)", row["title"]) for p in g.split(",")]
    terms = list(dict.fromkeys(t for t in (normalize(x) for x in [row["title"], *synonyms, *parts]) if len(t) >= 5))
    acronyms = list(dict.fromkeys(p.upper() for p in [*parts, *synonyms] if _ACRONYM.match(p.strip())
                                  and sum(c.isalpha() for c in p) >= 2))
    return terms, acronyms


def matches(title: str, text: str, terms: list[str], acronyms: list[str]) -> bool:
    padded = f" {normalize(f'{title} {text}')} "
    if any(f" {term} " in padded for term in terms):
        return True
    return any(re.search(rf"(?<![\w]){re.escape(a)}(?![\w])", f"{title} {text}") for a in acronyms)


def domain_of(topic: str) -> str:
    return next((tag for key, tag in DOMAINS if key in topic.lower()), "other")


def md(caller: str) -> tuple[tuple[str, str], ...]:
    return (("x-correlation-id", str(uuid.uuid4())), ("x-caller", caller))


async def run_topic(index: int, topic: str, run_id: str, settings: CollectorSettings) -> dict:
    async with grpc.aio.insecure_channel(os.environ.get("WS_INSIGHT_ADDR", "insight:50053")) as channel:
        expansion = await insight_pb2_grpc.InsightServiceStub(channel).ExpandQuery(
            insight_pb2.ExpandQueryRequest(query_text=topic), timeout=180, metadata=md("strategy-check"))
    address = os.environ.get("WS_COLLECTOR_SELF_ADDR", f"localhost:{settings.grpc_port}")
    async with grpc.aio.insecure_channel(address) as channel:
        stub = collector_pb2_grpc.CollectorServiceStub(channel)
        started = await stub.StartCollection(collector_pb2.StartCollectionRequest(
            idempotency_key=f"strategy-check:{run_id}:{index}", query_text=topic,
            terms=collector_pb2.SearchTerms(ru=list(expansion.ru_terms), en=list(expansion.en_terms),
                                            technologies=list(expansion.technologies)),
            mode=collector_pb2.COLLECTION_MODE_SEARCH,
            limits=collector_pb2.CollectionLimits(time_budget_seconds=240, max_total_documents=800)),
            timeout=60, metadata=md("strategy-check"))
        status = None
        for _ in range(240):
            status = await stub.GetCollection(collector_pb2.GetCollectionRequest(collection_id=started.collection_id),
                                              timeout=30, metadata=md("strategy-check"))
            if status.status in TERMINAL:
                break
            await asyncio.sleep(5)
        documents, token = [], ""
        while True:
            last = False
            async for chunk in stub.StreamDocuments(collector_pb2.StreamDocumentsRequest(
                    collection_id=started.collection_id, chunk_size=200, page_token=token), timeout=120,
                    metadata=md("strategy-check")):
                documents.extend(chunk.documents)
                token, last = chunk.next_page_token, chunk.last
            if last or not token:
                break
    with psycopg.connect(settings.pg_dsn) as conn:
        row = conn.execute("SELECT terms_technologies, technologies_mined, technologies_emerging FROM collector.collections "
                           "WHERE collection_id = %s", (started.collection_id,)).fetchone()
    runs = {common_pb2.SourceKey.Name(r.source_key).removeprefix("SOURCE_KEY_").lower():
            (common_pb2.OperationStatus.Name(r.status).removeprefix("OPERATION_STATUS_"), r.documents_found, r.error_code)
            for r in status.adapter_runs}
    return {"topic": topic, "collection_id": started.collection_id, "documents": documents,
            "model": list(row[0] or []), "mined": list(row[1] or []), "emerging": list(row[2] or []), "runs": runs}


async def main() -> None:
    paths = sys.argv[1:4] + ["/tmp/case_topics.txt", "/tmp/dataset.jsonl", "/tmp/synonyms.yaml"][len(sys.argv[1:4]):]
    topics = [line.strip() for line in open(paths[0], encoding="utf-8") if line.strip()]
    experts = [r for r in (json.loads(l) for l in open(paths[1], encoding="utf-8") if l.strip()) if int(r.get("label") or 0) == 1]
    synonyms = yaml.safe_load(open(paths[2], encoding="utf-8"))["synonyms"]
    settings, run_id, started = CollectorSettings(), time.strftime("%Y%m%d-%H%M%S"), time.monotonic()
    report, rows, total_found, total_experts, url_total = [], [], 0, 0, 0
    by_source: dict[str, int] = {}
    for index, topic in enumerate(topics):
        result = await run_topic(index, topic, run_id, settings)
        domain, found = domain_of(topic), 0
        docs = result["documents"]
        for row in (r for r in experts if r["domain_tag"] == domain):
            terms, acronyms = expert_terms(row, synonyms.get(row["row_id"]) or [])
            wanted = {url_key(u) for u in row["source_urls"]}
            hits = [d for d in docs if url_key(d.url) in wanted or matches(d.title, d.text, terms, acronyms)]
            sources = sorted({common_pb2.SourceKey.Name(d.source_key).removeprefix("SOURCE_KEY_").lower() for d in hits})
            url_hit = any(url_key(d.url) in wanted for d in docs)
            found += bool(hits)
            url_total += url_hit
            for source in sources:
                by_source[source] = by_source.get(source, 0) + 1
            rows.append([topic, row["row_id"], row["title"], len(hits), int(url_hit), ";".join(sources)])
        count = sum(1 for r in experts if r["domain_tag"] == domain)
        total_found, total_experts = total_found + found, total_experts + count
        report.append({k: v for k, v in result.items() if k != "documents"} | {"documents": len(docs), "found": found,
                                                                                "experts": count})
        failed = {k: v for k, v in result["runs"].items() if v[0] not in ("COMPLETED",)}
        print(f"{topic[:48]:48} | документов {len(docs)} | эксперты {found}/{count}"
              + (f" | источники с отказом: {failed}" if failed else ""), flush=True)
        print(f"    из корпусов ({len(result['emerging'])}): {result['emerging'][:10]}", flush=True)
        print(f"    извлечённые ({len(result['mined'])}): {result['mined'][:8]}", flush=True)
        print(f"    от модели: {len(result['model'])}", flush=True)
    json.dump({"totals": {"experts": total_experts, "found": total_found, "organizer_url": url_total, "by_source": by_source},
               "topics": report}, open("/tmp/strategy_check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open("/tmp/strategy_check.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["topic", "row_id", "title", "documents_matched", "organizer_url", "sources"])
        writer.writerows(rows)
    print(f"\nИТОГО за {time.monotonic() - started:.0f} с: экспертных сигналов найдено {total_found}/{total_experts}; "
          f"ссылок организаторов: {url_total}; по источникам: {dict(sorted(by_source.items()))}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
