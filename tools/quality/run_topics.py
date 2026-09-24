#!/usr/bin/env python3
"""Темы из файла по одной: полное время от приёма, карточки и источники — в JSON и CSV для разметки."""
import csv, json, sys, time, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path

API = "http://127.0.0.1:8080"
TOPICS = [t.strip() for t in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if t.strip()]
TOP_N = 10
KEY = next((l.split("=", 1)[1].strip() for l in reversed(Path(".env").read_text(encoding="utf-8").splitlines())
            if l.startswith("WS_API_KEY=")), "")

def call(method, path, body=None, headers=None):
    request = urllib.request.Request(f"{API}{path}", data=json.dumps(body).encode() if body else None, method=method)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if KEY:
        request.add_header("X-API-Key", KEY)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
rows, runs = [], []
for number, topic in enumerate(TOPICS, 1):
    print(f"[{number}/{len(TOPICS)}] {topic}", flush=True)
    status, job = call("POST", "/api/v1/queries", {"query_text": topic, "top_n": TOP_N},
                       {"Idempotency-Key": f"own-{stamp}-{number}"})
    if not isinstance(job, dict) or "job_id" not in job:
        runs.append({"topic": topic, "error": f"не принято: {status} {job}"})
        continue
    job_id, started = job["job_id"], time.monotonic()
    while True:
        _, job = call("GET", f"/api/v1/jobs/{job_id}")
        if isinstance(job, dict) and job.get("status") in ("COMPLETED", "PARTIAL", "FAILED", "CANCELLED"):
            break
        if time.monotonic() - started > 25 * 60:
            break
        time.sleep(5)
    _, results = call("GET", f"/api/v1/jobs/{job_id}/results")
    items = results.get("items", []) if isinstance(results, dict) else []
    cards = []
    for item in items:
        _, full = call("GET", f"/api/v1/results/items/{item['item_id']}")
        if isinstance(full, dict):
            cards.append(full)
            sources = full.get("sources") or []
            rows.append({
                "topic": topic, "rank": full.get("rank"), "title": full.get("title_ru"),
                "status": full.get("narrative_status"), "score": round((full.get("score") or 0) * 100, 1),
                "sources": ",".join(sorted({s.get("source_key", "") for s in sources})),
                "high_trust": any(s.get("trust_level") == "HIGH" for s in sources),
                "urls": " ".join(s.get("url", "") for s in sources[:3]),
                "label": "", "comment": "",
            })
    runs.append({"topic": topic, "job": job, "cards": cards,
                 "stats": results.get("stats") if isinstance(results, dict) else None})
    print(f"    статус {job.get('status') if isinstance(job, dict) else '—'}, карточек {len(cards)}", flush=True)
Path(f"own-topics-{stamp}.json").write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")
with open(f"relevance-{stamp}.csv", "w", encoding="utf-8", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["topic"])
    writer.writeheader()
    writer.writerows(rows)
print(f"сохранено: own-topics-{stamp}.json, relevance-{stamp}.csv")
