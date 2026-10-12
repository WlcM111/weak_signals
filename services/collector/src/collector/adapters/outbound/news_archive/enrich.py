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
from urllib.parse import urlsplit

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
    summary = _BANNER.sub(" ", " ".join([description, *paragraphs])).strip()[:1500]  # этап 5.0 (F11)
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

    async def run(self, limit: int, from_collections: bool = False) -> tuple[int, int]:
        """Возвращает (сколько записей получили текст, сколько не удалось).

        Этап 4.7: `from_collections` — сначала записи, которые попали в сборы.
        Этап 5.0 (аудит round2 F11, F12): страница проверки «вы человек?», входа или подписки — статус blocked, а не
        текст статьи; временный сбой (таймаут, сеть, 429, 5xx) — статус retry с нарастающей паузой (30 мин × 2^n, до
        суток), после MAX_ATTEMPTS — failed. Записи из карт сайтов (origin sitemap) читаются с самого сайта.
        """
        done = failed = 0
        entries = (await self._store.entries_from_collections(limit) if from_collections
                   else await self._store.entries_to_enrich(self._cues, limit))
        for entry in entries:
            await self._clock.sleep(self._pause)
            if entry.origin == "sitemap":
                url, hosts = entry.url, frozenset({(urlsplit(entry.url).hostname or "").lower()})
            else:
                stamp = entry.captured_at.strftime("%Y%m%d%H%M%S") if entry.captured_at else "2"
                url, hosts = WAYBACK_RAW.format(stamp=stamp, url=entry.url), WAYBACK_HOSTS
            page, transient, overloaded = "", False, False
            try:
                page = await self._http.get_text(url, allowed_hosts=hosts)
            except HttpStatusError as error:
                transient = error.status == 429 or error.status >= 500
                overloaded = transient and entry.origin != "sitemap"
            except (HttpTimeoutError, HttpTransportError):
                transient = True
            if transient:
                attempts = entry.enrich_attempts + 1
                status = "retry" if attempts < MAX_ATTEMPTS else "failed"
                retry = min(24 * 60, 30 * 2 ** entry.enrich_attempts) if status == "retry" else 0
                await self._store.save_enrichment(entry.url, "", "", None, status, [], retry)
                failed += 1
                if overloaded:
                    self._report("архив ответил отказом — остановка, продолжение повторным запуском")
                    break
                continue
            title, summary, published = extract_article(page) if page else ("", "", None)
            quality = page_quality(page, summary)
            status = {"ok": "page", "blocked": "blocked", "empty": "failed"}[quality]
            await self._store.save_enrichment(entry.url, title if quality == "ok" else "", summary if quality == "ok" else "",
                                              published if quality == "ok" else None, status,
                                              tokens_of(f"{title} {summary[:1500]}") if quality == "ok" else [])
            done, failed = (done + 1, failed) if status == "page" else (done, failed + 1)
            if (done + failed) % 50 == 0:
                self._report(f"обработано {done + failed}: с текстом {done}, без текста {failed}")
        return done, failed


MAX_ATTEMPTS = 5
_BLOCKED = re.compile(
    r"verify (that )?you are (a )?human|are you a robot|captcha|access denied|enable javascript|please sign in|"
    r"subscribe to (continue|read)|log in to continue|checking your browser|attention required|request blocked|"
    r"доступ запрещ|подтвердите, что вы не робот", re.I)
_BANNER = re.compile(r"[^.]*\b(your browser (is )?(out of date|outdated|not supported)|unsupported browser|"
                     r"update your browser|we use cookies)[^.]*\.?", re.I)


def page_quality(page: str, summary: str) -> str:
    """ok — текст статьи; blocked — страница проверки доступа, входа или подписки; empty — текста нет."""
    if not summary:
        return "blocked" if _BLOCKED.search(page or "") else "empty"
    if _BLOCKED.search(summary) and len(summary) < 600:
        return "blocked"
    return "ok"
