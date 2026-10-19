"""Опыт R-022(b)/R-102 (этап 5.3): число публикаций OpenAlex по годам для терминов карточек.

Запуск в контейнере collector (там ключ OpenAlex и сеть): python openalex_history.py terms.jsonl history.json [--backoff]
--backoff (итерация 2): дополнительно запрашиваются «хвосты» терминов без первых слов, не короче двух слов
(«aerospace composite defect detection» → «composite defect detection» → «defect detection»). Первая итерация
показала: 64 % терминов LLM слишком составные, точная фраза почти не встречается, и история области пустая.
Для каждого уникального термина (`core_en`, `task_en`) — один запрос с группировкой по году публикации, фразовый
поиск по названию и аннотации. Результаты копятся в history.json: при отказе источника (лимит) повторный запуск
продолжит с того же места. Код выхода 4 — источник ограничил запросы, данные неполные.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.openalex.org/works"


def counts(phrase: str, key: str, mailto: str) -> dict[str, int]:
    clean = phrase.replace(",", " ").replace('"', " ").strip()
    params = {"filter": f'title_and_abstract.search:"{clean}"', "group_by": "publication_year", "mailto": mailto}
    if key:
        params["api_key"] = key
    request = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": f"weak-signals-maturity-check (mailto:{mailto})"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - фиксированный адрес OpenAlex
        data = json.load(response)
    return {str(group["key"]): int(group["count"]) for group in data.get("group_by", []) if str(group["key"]).isdigit()}


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--backoff"]
    backoff = "--backoff" in sys.argv[1:]
    if len(args) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    terms_path, history_path = args[0], Path(args[1])
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
    phrases = set()
    for line in open(terms_path, encoding="utf-8"):
        if line.strip():
            row = json.loads(line)
            for phrase in (row.get("core_en", ""), row.get("task_en", "")):
                words = phrase.strip().lower().split()
                if not words:
                    continue
                phrases.add(" ".join(words))
                if backoff:
                    phrases.update(" ".join(words[i:]) for i in range(1, len(words) - 1))
    todo = sorted(p for p in phrases if p not in history)
    key, mailto = os.environ.get("WS_OPENALEX_API_KEY", ""), os.environ.get("WS_CONTACT_EMAIL", "team@example.org")
    print(f"терминов: {len(phrases)}, уже есть: {len(phrases) - len(todo)}, запросить: {len(todo)}", flush=True)
    status = 0
    for index, phrase in enumerate(todo, 1):
        try:
            history[phrase] = counts(phrase, key, mailto)
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                print(f"OpenAlex ограничил запросы (HTTP {error.code}) на {index}-м термине — данные неполные, "
                      "повторите запуск позже", flush=True)
                status = 4
                break
            history[phrase] = {"error": f"HTTP {error.code}"}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            history[phrase] = {"error": type(error).__name__}
        if index % 25 == 0:
            history_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
            print(f"запрошено {index} из {len(todo)}", flush=True)
        time.sleep(0.25)
    history_path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    failed = sum(1 for value in history.values() if "error" in value)
    print(f"готово: терминов {len(history)}, с ошибкой {failed} → {history_path}")
    return status


if __name__ == "__main__":
    sys.exit(main())
