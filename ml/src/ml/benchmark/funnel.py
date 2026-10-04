"""Воронка задания по снимку: где выбывают документы и кандидаты (аудит A3, этап 2).

Отвечает на два вопроса этапа: B2-a — источник «не опрошен» отличается от «опрошен, пусто»; B4 — сколько кандидатов
отсекают порог близости кандидата (`min_candidate_query_sim`, на стенде 0,88) и размер пула рубрики. Решение по B4
принимается на этапе 3; здесь — измерение.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ml.benchmark.snapshot import Snapshot

NOT_POLLED, FAILED, RATE_LIMITED, INCOMPLETE, EMPTY, OK, PARTIAL = (
    "NOT_POLLED", "FAILED", "RATE_LIMITED", "INCOMPLETE", "EMPTY", "OK", "PARTIAL")
OUTCOME_RU = {NOT_POLLED: "не опрошен", FAILED: "отказ", RATE_LIMITED: "лимит частоты", INCOMPLETE: "не завершён",
              EMPTY: "опрошен, пусто", OK: "есть документы", PARTIAL: "частично"}


def source_outcome(status: str, http_requests: int, documents_found: int, error_code: str | None) -> str:
    """Итог опроса источника (B2-a): «не опрошен» не смешивается с «опрошен, пусто».

    Ноль HTTP-запросов — всегда «не опрошен», какой бы статус ни стоял (COMPLETED без запросов — тоже).
    """
    code = (error_code or "").upper()
    status = (status or "").upper()
    if http_requests <= 0:
        return NOT_POLLED
    if code == "RATE_LIMITED":
        return PARTIAL if documents_found else RATE_LIMITED
    if code == "BUDGET_EXHAUSTED" or status == "CANCELLED":
        return PARTIAL if documents_found else INCOMPLETE
    if status == "FAILED" or code:
        return PARTIAL if documents_found else FAILED
    if status == "PARTIAL":
        return PARTIAL
    return OK if documents_found else EMPTY


def funnel(snapshot: Snapshot) -> dict[str, Any]:
    """Сводка воронки одного задания по всем этапам с причинами выбывания."""
    report: dict[str, Any] = {"snapshot": snapshot.name, "topic": snapshot.topic, "job_id": snapshot.job_id}
    collection = snapshot.json("collection.json") if snapshot.has("collection.json") else {}
    events = snapshot.jsonl("document_events.jsonl")
    responses = snapshot.jsonl("raw_responses.jsonl")
    by_event: dict[str, Counter[str]] = {}
    for event in events:
        by_event.setdefault(event["source_key"], Counter())[event["event"]] += 1
    by_response: dict[str, Counter[str]] = {}
    for response in responses:
        by_response.setdefault(response["source_key"], Counter())["OK" if response["outcome"] == "OK" else "ERROR"] += 1
    sources = {}
    for run in collection.get("adapter_runs") or []:
        key = run["source_key"]
        outcome = source_outcome(run.get("status", ""), int(run.get("http_requests") or 0),
                                 int(run.get("documents_found") or 0), run.get("error_code"))
        answered = by_response.get(key, Counter())
        sources[key] = {"status": run.get("status"), "error_code": run.get("error_code"),
                        "http_requests": run.get("http_requests"), "documents_found": run.get("documents_found"),
                        "documents_new": run.get("documents_new"), "outcome": outcome, "outcome_ru": OUTCOME_RU[outcome],
                        "events": dict(by_event.get(key, Counter())),
                        "responses_ok": answered["OK"], "responses_error": answered["ERROR"],
                        "success_share": round(answered["OK"] / sum(answered.values()), 4) if answered else None}
    row = collection.get("collection") or {}
    report["collection"] = {
        "documents_total": row.get("documents_total"), "status": row.get("status"),
        "published_since_year": row.get("published_since_year"), "time_budget_seconds": row.get("time_budget_seconds"),
        "max_total_documents": row.get("max_total_documents"), "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"), "events": dict(Counter(event["event"] for event in events)),
        "responses": len(responses), "sources": sources,
        "not_polled": sorted(k for k, v in sources.items() if v["outcome"] == NOT_POLLED),
        "empty": sorted(k for k, v in sources.items() if v["outcome"] == EMPTY),
    }
    capture = snapshot.json("capture.json") if snapshot.has("capture.json") else {}
    f = capture.get("funnel") or {}
    scored, selected = f.get("clusters_scored") or [], f.get("clusters_selected") or []
    selected_keys = {tuple(sorted(cluster)) for cluster in selected}
    analysis = snapshot.json("analysis.json") if snapshot.has("analysis.json") else {}
    candidates = analysis.get("candidates") or []
    config = capture.get("config") or {}
    report["analysis"] = {
        "documents_input": len(f.get("documents_input") or []), "truncated": f.get("truncated"),
        "near_dup_dropped": len(f.get("near_dup_dropped") or []),
        "relevance_dropped": len(f.get("relevance_dropped") or []), "min_doc_query_sim": f.get("min_doc_query_sim"),
        "clusters_total": f.get("clusters_total"), "small_cluster_documents": len(f.get("small_cluster_documents") or []),
        "clusters_scored": len(scored), "clusters_selected": len(selected),
        "clusters_cut_by_max_candidates": sum(1 for c in scored if tuple(sorted(c)) not in selected_keys),
        "max_candidates": f.get("max_candidates"), "candidates": len(candidates),
        "min_candidate_query_sim": config.get("min_candidate_query_sim"),
        "candidates_by_reason": dict(Counter(str(c.get("decision_reason")) for c in candidates)),
        "below_candidate_query_sim": sum(1 for c in candidates if c.get("decision_reason") == "LOW_QUERY_RELEVANCE"),
    }
    narrate = (snapshot.json("trace.json") or {}).get("narrate") or {} if snapshot.has("trace.json") else {}
    judged = narrate.get("judged") or {}
    report["selection"] = {
        "path": narrate.get("path"), "rubric_pool": (narrate.get("config") or {}).get("rubric_pool"),
        "weak": len((narrate.get("candidates") or {}).get("weak") or []),
        "excluded_by_analyzer": len((narrate.get("candidates") or {}).get("excluded") or []),
        "pool": len(narrate.get("pool") or []), "cut_by_pool": len(narrate.get("rest") or []),
        "cut_by_pool_low_query_relevance": sum(1 for r in narrate.get("rest") or [] if r[2] == "LOW_QUERY_RELEVANCE"),
        "prefiltered": len(narrate.get("prefiltered") or []), "judged": len(judged),
        "codes": dict(Counter(v.get("code") for v in judged.values())), "unjudged": len(narrate.get("unjudged") or []),
        "accepted_r": len(narrate.get("accepted") or []), "uncertain_fill": len(narrate.get("uncertain_fill") or []),
        "below_top_n": len(narrate.get("below") or []), "finalize_rounds": len(narrate.get("rounds") or []),
        "not_tried": len(narrate.get("not_tried") or []), "text_failed": len(narrate.get("text_failed") or []),
        "review_dropped": len(narrate.get("review_dropped") or {}), "final_dropped": len(narrate.get("final_dropped") or {}),
        "fallback": narrate.get("fallback"), "deadline_cut": narrate.get("deadline_cut"),
        "shown": len(narrate.get("shown") or []),
    }
    return report


def render_markdown(reports: list[dict[str, Any]]) -> str:
    """Таблицы воронки по заданиям: сбор, анализ, отбор."""
    lines = ["# Воронка заданий (режим А)", "",
             "| Тема | Документов | Не опрошены | Пусто | Порог близости докум. | Почти-дубли | Кластеров (отсечено пулом analyzer) "
             "| Кандидатов | Ниже порога кандидата | Пул | Вне пула | R | Карточек |", "|" + "---|" * 13]
    for r in reports:
        c, a, s = r["collection"], r["analysis"], r["selection"]
        lines.append(f"| {r['topic']} | {c['documents_total']} | {', '.join(c['not_polled']) or '—'} | "
                     f"{', '.join(c['empty']) or '—'} | {a['relevance_dropped']} | {a['near_dup_dropped']} | "
                     f"{a['clusters_selected']} ({a['clusters_cut_by_max_candidates']}) | {a['candidates']} | "
                     f"{a['below_candidate_query_sim']} | {s['pool']} | {s['cut_by_pool']} | {s['accepted_r']} | {s['shown']} |")
    lines += ["", "## Источники", "", "| Тема | Источник | Итог опроса | Запросов (ошибок) | Найдено | Принято | Дубли | Лимит | Отбраковано |",
              "|" + "---|" * 9]
    for r in reports:
        for key, v in sorted(r["collection"]["sources"].items()):
            e = v["events"]
            lines.append(f"| {r['topic']} | {key} | {v['outcome_ru']} | {v['http_requests']} ({v['responses_error']}) | "
                         f"{v['documents_found']} | {e.get('ACCEPTED', 0)} | {e.get('DEDUP', 0)} | {e.get('SOURCE_CAP', 0)} | "
                         f"{e.get('REJECTED', 0)} |")
    return "\n".join(lines) + "\n"
