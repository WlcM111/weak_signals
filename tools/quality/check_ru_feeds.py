"""Проверка русскоязычных RSS-лент для collector и добавление рабочих в WS_COLLECTOR_RSS_FEEDS (.env).

Запуск из корня проекта:
  python3 tools/quality/check_ru_feeds.py          — только проверка, .env не меняется;
  python3 tools/quality/check_ru_feeds.py --write  — проверка и добавление рабочих лент в .env
                                                     (до записи копия .env: ~/ws-backups/env.bak-rss-<время>).
Лента рабочая, если отвечает кодом 200 и в начале ответа есть <rss, <feed или <rdf:RDF.
В .env пишется итоговый адрес после перенаправлений. Остальные строки .env не печатаются и не меняются.
"""

from __future__ import annotations

import argparse
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ENV = Path(".env")
KEY = "WS_COLLECTOR_RSS_FEEDS"
CANDIDATES = [
    ("N+1", "https://nplus1.ru/rss"),
    ("Элементы — новости науки", "https://elementy.ru/rss/news"),
    ("Naked Science", "https://naked-science.ru/feedrss.xml"),
    ("Наука и жизнь", "https://www.nkj.ru/rss/"),
    ("ПостНаука", "http://postnauka.ru/feed"),
    ("RB.ru — все материалы", "https://rb.ru/feeds/all/"),
    ("RB.ru — ИИ", "https://rb.ru/feeds/tag/ai/"),
    ("RB.ru — биотехнологии", "https://rb.ru/feeds/tag/biotech/"),
    ("RB.ru — беспилотники", "https://rb.ru/feeds/tag/drone/"),
    ("RB.ru — большие данные", "https://rb.ru/feeds/tag/bigdata/"),
    ("RB.ru — интернет вещей", "https://rb.ru/feeds/tag/iot/"),
    ("RB.ru — кибербезопасность", "https://rb.ru/feeds/tag/cybersecurity/"),
    ("RB.ru — космос", "https://rb.ru/feeds/tag/space/"),
    ("RB.ru — сделки", "https://rb.ru/feeds/tag/deal/"),
    ("Runet.News", "https://runet.news/feed.rss"),
    ("Хабр — робототехника", "https://habr.com/ru/rss/hub/robot/all/"),
    ("Ведомости — новости", "https://www.vedomosti.ru/rss/news"),
]
MAX_BYTES = 5 * 1024 * 1024
try:  # Python с python.org на macOS без certifi не доверяет корневым сертификатам
    import certifi

    _SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL = ssl.create_default_context()
_FEED_RE = re.compile(rb"<(rss|feed|rdf:RDF)[\s>]", re.IGNORECASE)
_ENTRY_RE = re.compile(rb"<(item|entry)[\s>]", re.IGNORECASE)


def _norm(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _contact(lines: list[str]) -> str:
    values = [line.split("=", 1)[1].strip() for line in lines if line.startswith("WS_CONTACT_EMAIL=")]
    return values[-1] if values and values[-1] else "team@example.org"


def check(url: str, user_agent: str) -> tuple[bool, int, str, int, str]:
    """(рабочая, код, итоговый адрес, число записей, пояснение)."""
    request = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=30, context=_SSL) as response:
            body = response.read(MAX_BYTES)
            code, final = response.status, response.geturl()
    except urllib.error.HTTPError as error:
        return False, error.code, url, 0, "ошибка HTTP"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, 0, url, 0, f"нет ответа: {getattr(error, 'reason', error)}"
    if code != 200:
        return False, code, final, 0, "не 200"
    if not _FEED_RE.search(body[:4096]):
        return False, code, final, 0, "ответ не RSS/Atom"
    return True, code, final, len(_ENTRY_RE.findall(body)), "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description="проверка русскоязычных RSS-лент для collector")
    parser.add_argument("--write", action="store_true", help="добавить рабочие ленты в .env")
    args = parser.parse_args()
    if not ENV.is_file():
        print("СТОП: нет файла .env — запустите из корня проекта")
        return 1
    lines = ENV.read_text(encoding="utf-8").splitlines(keepends=True)
    user_agent = f"weak-signals/1.0 (mailto:{_contact([line.rstrip() for line in lines])})"
    good: list[str] = []
    notes: list[str] = []
    for number, (name, url) in enumerate(CANDIDATES):
        if number:
            time.sleep(1.0)
        ok, code, final, entries, note = check(url, user_agent)
        print(f"{'OK    ' if ok else 'ОТКАЗ '} код {code:>3} записей {entries:>3}  {name}: {final}"
              + ("" if ok else f"  ({note})"))
        if ok:
            good.append(final)
        notes.append(note)
    print(f"рабочих лент: {len(good)} из {len(CANDIDATES)}")
    if any("CERTIFICATE_VERIFY_FAILED" in note for note in notes):
        print("Python не доверяет сертификатам HTTPS: запустите «Install Certificates.command» "
              "из папки /Applications/Python 3.x и повторите")
    if not args.write:
        return 0
    if not good:
        print("рабочих лент нет — .env не изменён")
        return 0
    index = [i for i, line in enumerate(lines) if line.startswith(f"{KEY}=")]
    if len(index) != 1:
        print(f"СТОП: строк {KEY} в .env: {len(index)} (номера {[i + 1 for i in index]}), нужна ровно одна; "
              ".env не изменён")
        return 1
    line = lines[index[0]]
    ending = "\n" if line.endswith("\n") else ""
    current = [item.strip() for item in line.rstrip("\r\n").split("=", 1)[1].split(",") if item.strip()]
    known = {_norm(item) for item in current}
    added = []
    for url in good:
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.hostname and _norm(url) not in known:
            added.append(url)
            known.add(_norm(url))
    if not added:
        print("добавлять нечего: все рабочие ленты уже есть в .env")
        return 0
    backup_dir = Path.home() / "ws-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"env.bak-rss-{stamp}"
    number = 2
    while backup.exists():  # два запуска в одну секунду не должны затирать прежнюю копию
        backup = backup_dir / f"env.bak-rss-{stamp}-{number}"
        number += 1
    shutil.copy2(ENV, backup)
    lines[index[0]] = f"{KEY}={','.join(current + added)}{ending}"
    ENV.write_text("".join(lines), encoding="utf-8")
    print(f"ДОБАВЛЕНО в .env: {len(added)}; всего лент: {len(current) + len(added)}; копия .env: {backup}")
    for url in added:
        print(f"  + {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
