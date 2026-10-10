"""Быстрый замер стратегии поиска (этап 4): сколько технологий из экспертного списка находит сбор — без LLM-оценки,
без analyzer и без разметки, примерно за 25 минут на 6 тем.

Запускается внутри контейнера collector (тот же сетевой путь и ключи, что у сбора). Для каждой темы кейса:
1. ExpandQuery в insight (prompt expand_v2): фразы темы и технологии от модели;
2. широкий проход настоящими адаптерами по фразам темы (GDELT не трогается — его маршрут настраивается отдельно);
3. извлечение технологий из заголовков широкого прохода (domain/technology_mining, статистика без LLM);
4. прицельный проход по технологиям (search_targeted: релевантность в окне дат, малая квота на технологию).
Экспертный список в запросы не попадает: он нужен только для сопоставления после сбора. Совпадение — название
технологии или её синоним в заголовке или тексте документа (нормализация протокола match-v1) либо ссылка
организаторов. Итог: /tmp/strategy_check.json и /tmp/strategy_check.csv; в базу ничего не пишется.

Аргументы: файл тем, датасет, синонимы (по умолчанию /tmp/case_topics.txt, /tmp/dataset.jsonl, /tmp/synonyms.yaml).
"""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import json
import os
import re
import sys
import time
import unicodedata
import uuid

import grpc
import yaml

from collector.adapters.outbound.http_client import HttpxClient
from collector.adapters.outbound.rate_limiter import TokenBucketRateLimiter
from collector.config import CollectorSettings
from collector.domain.errors import AdapterFailure
from collector.domain.source_policy import SourcePolicy
from collector.domain.technology_mining import mine_technologies, targeted_since
from collector.domain.values import CollectionLimits, CollectionMode, SearchTerms, SourceKey
from collector.main import SLOW_SOURCE_TIMEOUT_SECONDS, _create_adapter
from weaksignals.insight.v1 import insight_pb2, insight_pb2_grpc
from ws_common.clock import SystemClock

BROAD = (SourceKey.OPENALEX, SourceKey.ARXIV, SourceKey.SEMANTIC_SCHOLAR, SourceKey.GITHUB, SourceKey.ZENODO,
         SourceKey.ROSPATENT, SourceKey.HACKERNEWS, SourceKey.RSS)
BROAD_SECONDS, TARGETED_SECONDS = 120, 150
BROAD_PER_SOURCE, RSS_PER_SOURCE, PER_TERM, MINED, MAX_TECH = 60, 150, 3, 15, 40
DOMAINS = (("индустриальн", "industrial_ai"), ("робототехник", "robotics"), ("инфраструктур", "ai_infrastructure"),
           ("финтех", "fintech"), ("безопасност", "ai_security"), ("edge", "edge"))
RATES = {SourceKey.ARXIV: 0.33, SourceKey.SEMANTIC_SCHOLAR: 0.5, SourceKey.OPENALEX: 5.0, SourceKey.GITHUB: 0.4,
         SourceKey.HACKERNEWS: 1.0, SourceKey.ZENODO: 1.0, SourceKey.ROSPATENT: 1.0, SourceKey.RSS: 2.0}
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return " ".join(_NON_WORD.sub(" ", text).replace("_", " ").split())


def url_key(url: str) -> str:
    url = re.sub(r"^https?://(www\.)?", "", (url or "").strip().lower()).split("#")[0].split("?")[0].rstrip("/")
    arxiv = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url)
    return f"arxiv:{arxiv.group(1)}" if arxiv else url


def expert_terms(row: dict, synonyms: list[str]) -> list[str]:
    found = [part.strip() for group in re.findall(r"\(([^)]*)\)", row["title"]) for part in group.split(",")]
    return list(dict.fromkeys(t for t in (normalize(x) for x in [*synonyms, *found]) if len(t) >= 5))


def mentions(text: str, terms: list[str]) -> bool:
    padded = f" {normalize(text)} "
    return any(f" {term} " in padded for term in terms)


def domain_of(topic: str) -> str:
    low = topic.lower()
    return next((tag for key, tag in DOMAINS if key in low), "other")


async def expand(topic: str) -> tuple[list[str], list[str], list[str]]:
    address = os.environ.get("WS_INSIGHT_ADDR", "insight:50053")
    async with grpc.aio.insecure_channel(address) as channel:
        stub = insight_pb2_grpc.InsightServiceStub(channel)
        response = await stub.ExpandQuery(insight_pb2.ExpandQueryRequest(query_text=topic), timeout=120,
                                          metadata=(("x-correlation-id", str(uuid.uuid4())), ("x-caller", "strategy-check")))
    return list(response.ru_terms), list(response.en_terms), list(response.technologies)


async def drain(key, iterator, sink: list, failures: dict) -> None:  # noqa: ANN001
    try:
        async for document in iterator:
            sink.append((key.value, document))
    except AdapterFailure as failure:
        failures[key.value] = f"{failure.code}: {failure.message[:120]}"
    except Exception as error:  # noqa: BLE001 — отказ одного источника не останавливает замер
        failures[key.value] = f"{type(error).__name__}: {str(error)[:120]}"


async def run_topic(topic: str, settings, http, slow, limiter, clock) -> dict:  # noqa: ANN001
    ru, en, model_tech = await expand(topic)
    adapters = {}
    for key in BROAD:
        try:
            adapter = _create_adapter(key, settings, http, limiter, clock, slow, None)
        except ValueError:
            continue
        adapter.policy = settings.source_policies.get(key, SourcePolicy())
        adapters[key] = adapter
    base = CollectionLimits.defaults(CollectionMode.SEARCH)
    terms = SearchTerms(ru=tuple(ru), en=tuple(en), technologies=tuple(model_tech))
    broad: list = []
    failures: dict = {}
    deadline = clock.monotonic() + BROAD_SECONDS
    await asyncio.gather(*(drain(key, adapter.search(terms, dataclasses.replace(
        base, max_documents_per_source=RSS_PER_SOURCE if key is SourceKey.RSS else BROAD_PER_SOURCE), deadline),
        broad, failures) for key, adapter in adapters.items()))
    titles = [(document.title, key) for key, document in broad]
    mined = mine_technologies(titles, exclude=(*en, *ru, *model_tech), limit=MINED)
    merged = list(dict.fromkeys([*model_tech, *mined]))[:MAX_TECH]
    since = targeted_since(clock.now(), 24, base.published_since_year)
    targeted: list = []
    deadline = clock.monotonic() + TARGETED_SECONDS
    await asyncio.gather(*(drain(key, adapter.search_targeted(merged, PER_TERM, since, deadline), targeted, failures)
                           for key, adapter in adapters.items() if getattr(adapter, "supports_targeted", False)))
    return {"topic": topic, "ru_terms": ru, "en_terms": en, "model_technologies": model_tech, "mined_technologies": mined,
            "broad": broad, "targeted": targeted, "failures": failures}


async def main() -> None:
    topics_path, dataset_path, synonyms_path = (sys.argv[1:4] + ["/tmp/case_topics.txt", "/tmp/dataset.jsonl",
                                                                 "/tmp/synonyms.yaml"][len(sys.argv[1:4]):])
    topics = [line.strip() for line in open(topics_path, encoding="utf-8") if line.strip()]
    experts = [r for r in (json.loads(l) for l in open(dataset_path, encoding="utf-8") if l.strip()) if int(r.get("label") or 0) == 1]
    synonyms = yaml.safe_load(open(synonyms_path, encoding="utf-8"))["synonyms"]
    settings, clock = CollectorSettings(), SystemClock()
    http = HttpxClient(contact_email=settings.contact_email, read_timeout_seconds=settings.collector_http_timeout_seconds,
                       max_bytes=settings.http_max_bytes, retries=settings.collector_http_retries)
    slow = HttpxClient(contact_email=settings.contact_email,
                       read_timeout_seconds=max(settings.collector_http_timeout_seconds, SLOW_SOURCE_TIMEOUT_SECONDS),
                       max_bytes=settings.http_max_bytes, retries=0)
    limiter = TokenBucketRateLimiter(rates=RATES, clock=clock)
    report, rows, totals = [], [], {"experts": 0, "broad": 0, "with_targeted": 0, "organizer_url": 0}
    started = time.monotonic()
    for topic in topics:
        result = await run_topic(topic, settings, http, slow, limiter, clock)
        domain = domain_of(topic)
        found_broad = found_all = 0
        for row in (r for r in experts if r["domain_tag"] == domain):
            terms = expert_terms(row, synonyms.get(row["row_id"]) or [])
            wanted = {url_key(u) for u in row["source_urls"]}
            in_broad = [k for k, d in result["broad"] if mentions(f"{d.title} {d.text}", terms)]
            in_targeted = [k for k, d in result["targeted"] if mentions(f"{d.title} {d.text}", terms)]
            url_hit = any(url_key(d.url) in wanted for _, d in result["broad"] + result["targeted"])
            found_broad += bool(in_broad or url_hit)
            found_all += bool(in_broad or in_targeted or url_hit)
            totals["organizer_url"] += url_hit
            rows.append([topic, row["row_id"], row["title"], len(in_broad), len(in_targeted), int(url_hit),
                         ";".join(sorted(set(in_broad + in_targeted)))])
        count = sum(1 for r in experts if r["domain_tag"] == domain)
        totals["experts"] += count
        totals["broad"] += found_broad
        totals["with_targeted"] += found_all
        report.append({key: value for key, value in result.items() if key not in ("broad", "targeted")}
                      | {"documents_broad": len(result["broad"]), "documents_targeted": len(result["targeted"]),
                         "experts": count, "found_broad": found_broad, "found_with_targeted": found_all})
        print(f"{topic[:48]:48} | технологий: модель {len(result['model_technologies'])}, извлечено "
              f"{len(result['mined_technologies'])} | документов: широкий {len(result['broad'])}, прицельный "
              f"{len(result['targeted'])} | эксперты: широкий {found_broad}/{count}, с прицельным {found_all}/{count}"
              + (f" | отказы: {result['failures']}" if result["failures"] else ""), flush=True)
        print(f"    технологии модели: {result['model_technologies'][:12]}", flush=True)
        print(f"    извлечённые: {result['mined_technologies']}", flush=True)
    json.dump({"totals": totals, "topics": report}, open("/tmp/strategy_check.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    with open("/tmp/strategy_check.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["topic", "row_id", "title", "broad_mentions", "targeted_mentions", "organizer_url", "sources"])
        writer.writerows(rows)
    print(f"\nИТОГО за {time.monotonic() - started:.0f} с: экспертных технологий найдено — широкий проход "
          f"{totals['broad']}/{totals['experts']}, с прицельным {totals['with_targeted']}/{totals['experts']}; "
          f"ссылок организаторов: {totals['organizer_url']}", flush=True)
    await http.aclose()
    await slow.aclose()


if __name__ == "__main__":
    asyncio.run(main())
