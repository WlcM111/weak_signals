#!/usr/bin/env python3
"""Stand report: resource checks -> run case topics -> compact ASCII report (all topics, cards, metrics, sources).

Runs on the server from the project root (/opt/weak-signals). Standard library only.
  python3 stand_report.py                      # checks + run 5 case topics + report (fintech from the first saved run)
  python3 stand_report.py --no-checks          # skip resource checks
  python3 stand_report.py --report-only FILE…  # build the report from saved run files, no API calls
Safety: only GET/POST to the local API (127.0.0.1:8080), read-only SELECT via psql, one light request per
external resource from inside the containers. The API key is read from .env and never printed.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API = "http://127.0.0.1:8080"
ACTIVE = {"QUEUED", "COLLECTING", "ANALYZING", "NARRATING"}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
FINTECH = "перспективные решения в финтехе"
OUT_DIR = Path("ml/reports/stand")
KEY = next((line.split("=", 1)[1].strip() for line in reversed(Path(".env").read_text(encoding="utf-8").splitlines())
            if line.startswith("WS_API_KEY=")), "") if Path(".env").is_file() else ""

TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
              "a b v g d e yo zh z i y k l m n o p r s t u f kh ts ch sh shch _ y _ e yu ya".split()))
TR = {k: ("" if v == "_" else v) for k, v in TR.items()}
PUNCT = {"«": '"', "»": '"', "—": "-", "–": "-", "…": "...", "“": '"', "”": '"', "’": "'", "№": "No", " ": " "}


def ascii_text(text: object, limit: int = 0) -> str:
    """Russian -> Latin transliteration, everything else forced to ASCII."""
    out = []
    for ch in str(text or ""):
        low = ch.lower()
        if low in TR:
            t = TR[low]
            out.append(t.capitalize() if ch != low and t else t)
        else:
            out.append(PUNCT.get(ch, ch))
    s = " ".join("".join(out).encode("ascii", "ignore").decode().split())
    return s[: limit - 1] + "~" if limit and len(s) > limit else s


def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, object]:
    request = urllib.request.Request(f"{API}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if KEY:
        request.add_header("X-API-Key", KEY)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, (error.read() or b"")[:300].decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, str(error)[:200]


def psql(query: str) -> list[list[str]]:
    try:
        result = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d",
                                 "weaksignals", "-At", "-F", "\t", "-c", query], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.split("\t") for line in result.stdout.splitlines() if line]


CHECK_CODE = r'''
import json, os, time, urllib.request, urllib.error
checks = json.loads(os.environ["WS_CHECKS"])
ua = "weak-signals-stand-check/1.0 (mailto:%s)" % os.environ.get("WS_CONTACT_EMAIL", "")
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
opener = urllib.request.build_opener(NoRedirect)
rows = []
for name, url, method, body, auth_env in checks:
    headers = {"User-Agent": ua, "Accept": "*/*"}
    if auth_env and os.environ.get(auth_env):
        headers["Authorization"] = "Bearer " + os.environ[auth_env]
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    start = time.time()
    try:
        with opener.open(req, timeout=20) as r:
            code, extra = r.status, len(r.read(65536))
    except urllib.error.HTTPError as e:
        code, extra = e.code, (e.headers.get("via", "") or "")[:40]
    except Exception as e:
        code, extra = 0, type(e).__name__ + ": " + str(e)[:80]
    rows.append([name, code, int((time.time() - start) * 1000), str(extra)])
    time.sleep(1)
print(json.dumps(rows))
'''


def resource_checks() -> list[list]:
    """One light request per external resource, from the container that really uses it."""
    groups = {
        "collector": [
            ["openalex", "https://api.openalex.org/works?search=edge%20ai&per-page=1", "GET", None, ""],
            ["arxiv", "https://export.arxiv.org/api/query?search_query=all:edge%20AND%20all:inference&max_results=1", "GET", None, ""],
            ["semantic_scholar", "https://api.semanticscholar.org/graph/v1/paper/search?query=edge%20inference&limit=1", "GET", None, ""],
            ["github", "https://api.github.com/search/repositories?q=edge%20inference&per_page=1", "GET", None, ""],
            ["zenodo", "https://zenodo.org/api/records?q=edge%20inference&size=1", "GET", None, ""],
            ["doi_resolver", "https://doi.org/10.1038/nature14539", "GET", None, ""],
            ["gdelt", "https://api.gdeltproject.org/api/v2/doc/doc?query=%22edge%20computing%22&mode=artlist&maxrecords=1&format=json", "GET", None, ""],
            ["wikimedia", "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Edge_computing/daily/20260901/20260907", "GET", None, ""],
            ["rospatent", "https://searchplatform.rospatent.gov.ru/patsearch/v0.2/search", "POST", {"qn": "нейросеть", "limit": 1}, "WS_ROSPATENT_TOKEN"],
        ],
        "insight": [
            ["gigachat_oauth_host", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth", "GET", None, ""],
            ["gigachat_api_host", "https://api.giga.chat/v1/models", "GET", None, ""],
        ],
        "analyzer": [
            ["huggingface_e5", "https://huggingface.co/api/models/intfloat/multilingual-e5-base", "GET", None, ""],
        ],
    }
    feeds = subprocess.run(["docker", "compose", "exec", "-T", "collector", "printenv", "WS_COLLECTOR_RSS_FEEDS"],
                           capture_output=True, text=True, timeout=30).stdout.strip()
    for index, feed in enumerate(f for f in feeds.split(",") if f.strip()):
        groups["collector"].append([f"rss_{index + 1}:{feed.split('/')[2]}", feed.strip(), "GET", None, ""])
    rows = []
    for service, checks in groups.items():
        result = subprocess.run(["docker", "compose", "exec", "-T", "-e", f"WS_CHECKS={json.dumps(checks)}", service,
                                 "python", "-"], input=CHECK_CODE, capture_output=True, text=True, timeout=600)
        try:
            rows += json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            rows.append([f"{service}:check_failed", 0, 0, (result.stderr or result.stdout)[-120:]])
    return rows


def verdict(name: str, code: int) -> str:
    if code == 0:
        return "UNREACHABLE"
    if 200 <= code < 400:
        return "OK"
    if code == 429:
        return "RATE_LIMITED"
    if code in (401, 403, 405) and name.startswith(("gigachat", "rospatent")):
        return "REACHABLE(auth)"
    return f"HTTP_{code}"


def run_topic(topic: str, top_n: int, idem: str) -> dict:
    status, accepted = call("POST", "/api/v1/queries", {"query_text": topic, "top_n": top_n}, {"Idempotency-Key": idem})
    job_id = accepted.get("job_id") if isinstance(accepted, dict) else None
    if not job_id:
        return {"topic": topic, "job": {"status": f"SUBMIT_FAILED_{status}", "error": ascii_text(accepted, 200)},
                "cards": [], "stats": None}
    started, job = time.time(), {}
    while time.time() - started < 1500:
        _, job = call("GET", f"/api/v1/jobs/{job_id}")
        if isinstance(job, dict) and job.get("status") not in ACTIVE:
            break
        time.sleep(10)
    _, results = call("GET", f"/api/v1/jobs/{job_id}/results")
    cards = []
    for item in (results.get("items") or []) if isinstance(results, dict) else []:
        _, full = call("GET", f"/api/v1/results/items/{item['item_id']}")
        cards.append(full if isinstance(full, dict) else item)
    return {"topic": topic, "job": job, "cards": cards, "stats": results.get("stats") if isinstance(results, dict) else None}


def adapter_runs(job_id: str) -> list[list[str]]:
    if not UUID_RE.match(job_id or ""):
        return []
    link = psql(f"SELECT collection_id FROM orchestrator.jobs WHERE job_id = '{job_id}'")
    cid = link[0][0] if link and UUID_RE.match(link[0][0]) else ""
    return psql("SELECT source_key, status, http_requests, documents_found, documents_new, coalesce(error_code, '') "
                f"FROM collector.adapter_runs WHERE collection_id = '{cid}' ORDER BY source_key") if cid else []


def minutes(job: dict) -> str:
    try:
        a = datetime.fromisoformat(str(job["started_at"]).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(job["finished_at"]).replace("Z", "+00:00"))
        return f"{(b - a).total_seconds() / 60:.1f}"
    except (KeyError, ValueError, TypeError):
        return "?"


def report(blocks: list[dict], checks: list[list], top_n: int, origin: dict[str, str]) -> list[str]:
    L = []
    _, model = call("GET", "/api/v1/model")
    mv = model.get("model_version_id", model.get("version", "?")) if isinstance(model, dict) else "?"
    L.append(f"== STAND REPORT {datetime.now():%Y-%m-%d %H:%M} | top_n={top_n} | model={ascii_text(mv)}")
    if checks:
        L.append("== RESOURCES name | http | ms | verdict | extra")
        L += [f"{r[0]} | {r[1]} | {r[2]} | {verdict(r[0], r[1])} | {ascii_text(r[3], 60)}" for r in checks]
    L.append("== SUMMARY topic | job | min | docs | cand | weak | conf75 | cards | src/card | types")
    all_cards, types_all = [], collections.Counter()
    for b in blocks:
        s, cards = b.get("stats") or {}, b.get("cards") or []
        types = collections.Counter(src.get("source_type", "?") for c in cards for src in c.get("sources") or [])
        types_all.update(types)
        all_cards += cards
        spc = f"{sum(len(c.get('sources') or []) for c in cards) / len(cards):.1f}" if cards else "0"
        L.append(f"{ascii_text(b['topic'], 40)} | {(b.get('job') or {}).get('status')} | {minutes(b.get('job') or {})} | "
                 f"{s.get('documents_collected', '?')} | {s.get('candidates_found', '?')} | {s.get('weak_signals_total', '?')} | "
                 f"{s.get('weak_signals_confident', '?')} | {len(cards)} | {spc} | "
                 + ",".join(f"{k[:4]}{v}" for k, v in types.most_common(5)) + f" | from:{origin.get(b['topic'], '?')}")
    if all_cards:
        sc = sorted(c.get("score", 0) for c in all_cards)
        L.append(f"TOTAL cards={len(all_cards)} conf_min={sc[0]:.2f} conf_med={sc[len(sc) // 2]:.2f} conf_max={sc[-1]:.2f} "
                 f"market_cards={sum(any(src.get('source_type') in ('INDUSTRY_MEDIA', 'NEWS') for src in c.get('sources') or []) for c in all_cards)} "
                 "types=" + ",".join(f"{k}:{v}" for k, v in types_all.most_common(8)))
    for n, b in enumerate(blocks, 1):
        job, s = b.get("job") or {}, b.get("stats") or {}
        L.append(f"== T{n} {ascii_text(b['topic'])} | job={job.get('status')} min={minutes(job)} "
                 f"expand_fallback={s.get('expand_used_fallback')} narr_gen={s.get('narratives_generated')} "
                 f"narr_fb={s.get('narratives_fallback')} http={s.get('http_requests_total')}")
        runs = adapter_runs(str(job.get("job_id", "")))
        if runs:
            L.append("adapters: " + " ; ".join(f"{r[0]}:{r[1][:4]} q{r[2]} f{r[3]} n{r[4]}" + (f" {r[5]}" if r[5] else "")
                                               for r in runs if len(r) >= 6))
        for c in b.get("cards") or []:
            preds = ", ".join(f"{ascii_text(f.get('feature_name'), 22)}{float(f.get('contribution') or 0):+.2f}"
                              for f in (c.get("features") or [])[:3])
            L.append(f" #{c.get('rank')} [{float(c.get('score') or 0):.2f} {c.get('confidence_band', '')}] "
                     f"{ascii_text(c.get('title_ru'), 110)}")
            L.append(f"    st={c.get('predicted_stage')} tr={c.get('predicted_trend')} narr={c.get('narrative_status')} "
                     f"srcs={len(c.get('sources') or [])} pred: {preds}")
            L.append(f"    desc: {ascii_text(c.get('description_ru'), 170)}")
            for src in c.get("sources") or []:
                dom = (str(src.get("url") or "").split("/") + ["", "", ""])[2][:28]
                L.append(f"    - {str(src.get('source_type', ''))[:4]} {str(src.get('published_at') or '')[:4]} "
                         f"{str(src.get('trust_level', ''))[:3]} {src.get('language_code', '')} {dom} | "
                         f"{ascii_text(src.get('title'), 95)}")
    L.append(f"== END lines={len(L) + 1}")
    return L


def first_fintech_block() -> tuple[dict | None, str]:
    """Самый ранний прогон финтеха, сделанный на этом сервере: файлы прогонов из репозитория (есть в git) не берутся."""
    try:
        tracked = set(subprocess.run(["git", "ls-files"], capture_output=True, text=True, timeout=30).stdout.split())
    except (OSError, subprocess.SubprocessError):
        tracked = set()
    for path in sorted((p for p in glob.glob("own-topics-*.json") if p not in tracked), key=os.path.getmtime):
        try:
            for block in json.loads(Path(path).read_text(encoding="utf-8")):
                if block.get("topic") == FINTECH and block.get("cards"):
                    return block, path
        except (OSError, ValueError):
            continue
    return None, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="stand report")
    parser.add_argument("--no-checks", action="store_true")
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--report-only", nargs="*", default=None)
    parser.add_argument("--all-topics", action="store_true", help="прогнать все темы, в том числе финтех")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    origin: dict[str, str] = {}
    if args.report_only is not None:
        blocks = [b for f in args.report_only for b in json.loads(Path(f).read_text(encoding="utf-8"))]
        origin = {b["topic"]: "saved" for b in blocks}
        checks = []
    else:
        checks = [] if args.no_checks else resource_checks()
        for r in checks:
            print(f"check {r[0]}: {r[1]} {verdict(r[0], r[1])}", flush=True)
        topics = [t.strip() for t in Path("ml/reports/rubric/case_topics.txt").read_text(encoding="utf-8").splitlines() if t.strip()]
        fin, fin_path = (None, "") if args.all_topics else first_fintech_block()
        blocks = []
        for topic in topics:
            if topic == FINTECH and fin is not None:
                blocks.append(fin)
                origin[topic] = Path(fin_path).name[11:26]
                print(f"topic {ascii_text(topic)}: taken from {fin_path}", flush=True)
                continue
            print(f"topic {ascii_text(topic)}: running...", flush=True)
            block = run_topic(topic, args.top_n, f"stand-{stamp}-{len(blocks) + 1}")
            blocks.append(block)
            origin[topic] = "new"
            print(f"  -> {block['job'].get('status')} cards={len(block['cards'])} {block['job'].get('error', '')}", flush=True)
        (OUT_DIR / f"stand-run-{stamp}.json").write_text(json.dumps(blocks, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = report(blocks, checks, args.top_n, origin)
    path = OUT_DIR / f"stand-report-{stamp}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="ascii", errors="replace")
    print("\n".join(lines))
    print(f"saved: {path} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
