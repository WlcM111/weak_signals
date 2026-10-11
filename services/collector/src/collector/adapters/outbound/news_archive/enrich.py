"""Текст статей архива из архивной копии страницы (этап 4.6, аудит P08).

Запись Wayback содержит только слова адреса: компании, механизм, параметры пилота есть лишь в тексте статьи, и поиск
по заголовку из адреса их не видит. Команда `archive enrich` берёт записи со статусом slug — сначала события
стартапов, затем новые, — загружает архивную копию страницы (`web.archive.org/web/<время>id_/<адрес>`, исходный HTML
без панели архива) и извлекает заголовок, описание, дату публикации и первые абзацы. Не удалось извлечь —
статус failed: запись остаётся metadata-only, её больше не запрашивают.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from datetime import date

from collector.application.ports import HttpClient, HttpStatusError, HttpTimeoutError, HttpTransportError, NewsArchiveStore
from collector.domain.text_tokens import tokens_of
from ws_common.clock import Clock

WAYBACK_RAW = "https://web.archive.org/web/{stamp}id_/{url}"
WAYBACK_HOSTS = frozenset({"web.archive.org"})
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_META = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR = re.compile(r'(\w[\w:-]*)\s*=\s*("([^"]*)"|\'([^\']*)\')', re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_PARAGRAPH = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_LD_DATE = re.compile(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_TIME = re.compile(r'<time[^>]+datetime="(\d{4}-\d{2}-\d{2})', re.I)


def _clean(text: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", text or "")).split())


def _metas(page: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for tag in _META.findall(page):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key and attrs.get("content") and key not in found:
            found[key] = _clean(attrs["content"])
    return found


def extract_article(page: str) -> tuple[str, str, date | None]:
    """Заголовок, текст (описание и первые абзацы, до 1500 символов) и дата публикации со страницы статьи."""
    page = _SCRIPT.sub(" ", page or "") if "datePublished" not in (page or "") else page or ""
    metas = _metas(page)
    title_match = _TITLE.search(page)
    title = metas.get("og:title") or metas.get("twitter:title") or (_clean(title_match.group(1)) if title_match else "")
    description = metas.get("og:description") or metas.get("description") or metas.get("twitter:description") or ""
    paragraphs = []
    for raw in _PARAGRAPH.findall(_SCRIPT.sub(" ", page)):
        text = _clean(raw)
        if len(text) >= 60 and text not in paragraphs and text != description:
            paragraphs.append(text)
        if len(paragraphs) >= 4:
            break
    summary = " ".join([description, *paragraphs]).strip()[:1500]
    published = None
    for candidate in (metas.get("article:published_time", "")[:10], metas.get("datepublished", "")[:10],
                      *(m.group(1) for m in [_LD_DATE.search(page), _TIME.search(page)] if m)):
        try:
            published = date.fromisoformat(candidate)
            break
        except ValueError:
            continue
    return title[:512], summary, published


class ArchiveEnricher:
    """Дополняет записи архива текстом их архивных копий."""

    def __init__(self, http: HttpClient, store: NewsArchiveStore, clock: Clock, cues: Sequence[str],
                 pause_seconds: float = 2.0, report: Callable[[str], None] = print) -> None:
        self._http, self._store, self._clock, self._cues = http, store, clock, tuple(cues)
        self._pause, self._report = pause_seconds, report

    async def run(self, limit: int) -> tuple[int, int]:
        """Возвращает (сколько записей получили текст, сколько не удалось)."""
        done = failed = 0
        for entry in await self._store.entries_to_enrich(self._cues, limit):
            stamp = entry.captured_at.strftime("%Y%m%d%H%M%S") if entry.captured_at else "2"
            await self._clock.sleep(self._pause)
            try:
                page = await self._http.get_text(WAYBACK_RAW.format(stamp=stamp, url=entry.url), allowed_hosts=WAYBACK_HOSTS)
            except HttpStatusError as error:
                if error.status == 429 or error.status >= 500:
                    self._report(f"архив ответил {error.status} — остановка, продолжение повторным запуском")
                    break
                page = ""
            except (HttpTimeoutError, HttpTransportError):
                page = ""
            title, summary, published = extract_article(page) if page else ("", "", None)
            status = "page" if summary else "failed"
            await self._store.save_enrichment(entry.url, title, summary, published, status,
                                              tokens_of(f"{title} {summary[:600]}"))
            done, failed = (done + 1, failed) if status == "page" else (done, failed + 1)
            if (done + failed) % 50 == 0:
                self._report(f"обработано {done + failed}: с текстом {done}, без текста {failed}")
        return done, failed
