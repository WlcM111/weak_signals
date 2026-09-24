"""Детерминированные правила нормализации документов (§9.2 ТЗ, §6 HANDOFF).

Все функции чистые: одинаковый вход → одинаковый выход, без обращений к сети, БД и часам.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_TITLE_LENGTH = 512
MAX_TEXT_LENGTH = 8000
MAX_URL_LENGTH = 2048
MAX_ORIGIN_DOMAIN_LENGTH = 255
MAX_RAW_META_BYTES = 8192
CONTENT_HASH_TEXT_PREFIX = 2000

_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "ref", "yclid", "mc_cid", "mc_eid"})
_WHITESPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_LANGUAGE_CODE_RE = re.compile(r"^[a-z]{2,3}$")
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)

UNKNOWN_LANGUAGE = "und"

try:  # langdetect — рабочая зависимость; отсутствие библиотеки не должно ломать нормализацию
    from langdetect import DetectorFactory, detect  # type: ignore[import-untyped]
    from langdetect.lang_detect_exception import LangDetectException  # type: ignore[import-untyped]

    DetectorFactory.seed = 0  # детерминированный результат между запусками
    _LANGDETECT_AVAILABLE = True
except ImportError:  # pragma: no cover - ветка выбирается составом окружения
    _LANGDETECT_AVAILABLE = False


def canonical_url(url: str) -> str:
    """Канонический URL: нижний регистр схемы/хоста, без `www.`, трекинговых параметров, фрагмента и хвостового `/`."""
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"URL не абсолютный: {url[:120]}")
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    host = host.lower().removeprefix("www.")
    if not host:
        raise ValueError(f"URL без хоста: {url[:120]}")
    netloc = host
    if parts.port and not ((scheme == "https" and parts.port == 443) or (scheme == "http" and parts.port == 80)):
        netloc = f"{host}:{parts.port}"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS
            and not key.lower().startswith(_TRACKING_PARAM_PREFIXES)
        ]
    )
    path = parts.path.rstrip("/") if parts.path != "/" else ""
    return urlunsplit((scheme, netloc, path, query, ""))


def origin_domain(url: str) -> str:
    """Хост URL без `www.` (поле `documents.origin_domain`, ≤ 255 символов)."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    if not host:
        raise ValueError(f"URL без хоста: {url[:120]}")
    return host[:MAX_ORIGIN_DOMAIN_LENGTH]


def url_hash(canonical: str) -> str:
    """sha256 канонического URL (UNIQUE-ключ дедупликации)."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    """Нормализация для хеша содержимого: NFKC, нижний регистр, без пунктуации, схлопнутые пробелы."""
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def content_hash(title: str, text: str) -> str:
    """sha256 нормализованного заголовка и первых 2000 символов текста (near-dup первого уровня)."""
    payload = f"{normalize_text(title)} {normalize_text(text[:CONTENT_HASH_TEXT_PREFIX])}".strip()
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_HTML_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]{0,500}>|<!--.*?-->", re.DOTALL)


def strip_html(value: str) -> str:
    """Текст без HTML-разметки и с раскрытыми сущностями (`&amp;`, `&#8230;`).

    RSS-ленты отдают описание записи как HTML; без очистки теги попадали в ключевые фразы,
    лексические признаки, промпты LLM и в интерфейс. Шаблон тега требует букву после `<`, поэтому
    математические записи вида `a < b` в аннотациях не затрагиваются.
    """
    if "<" not in value and "&" not in value:
        return value
    without_blocks = _HTML_BLOCK_RE.sub(" ", value)
    return html.unescape(_HTML_TAG_RE.sub(" ", without_blocks))


_XML_ENCODING_RE = re.compile(rb"^\s*<\?xml[^>]*encoding=[\"']([A-Za-z0-9._-]+)[\"']", re.IGNORECASE)


def decode_body(body: bytes) -> str:
    """Декодирует тело ответа источника: BOM → кодировка из объявления XML → UTF-8.

    Часть русскоязычных RSS-лент отдаётся в windows-1251; декодирование их как UTF-8 превращало
    текст в знаки замены и лишало ленту совпадений с поисковыми фразами.
    """
    if body.startswith(b"\xef\xbb\xbf"):
        return body[3:].decode("utf-8", errors="replace")
    match = _XML_ENCODING_RE.match(body[:300])
    encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def clean_whitespace(value: str) -> str:
    """Схлопывает пробелы в тексте внешнего источника.

    Управляющие символы (категория Cc: табуляция, NUL, перевод каретки) заменяются пробелом —
    иначе соседние слова склеиваются; форматирующие символы (Cf: zero-width space, soft hyphen)
    удаляются как невидимые.
    """
    cleaned: list[str] = []
    for char in value:
        category = unicodedata.category(char)
        if category == "Cc":
            cleaned.append(" ")
        elif category[0] != "C":
            cleaned.append(char)
    return _WHITESPACE_RE.sub(" ", "".join(cleaned)).strip()


def truncate(value: str, limit: int) -> str:
    """Усечение строки до лимита по границе слова, когда это возможно."""
    if len(value) <= limit:
        return value
    cut = value[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip()


def detect_language(text: str) -> str:
    """Код языка ISO 639-1 по тексту; `und`, если определить не удалось (§6 HANDOFF).

    Основной определитель — langdetect; резервный — соотношение кириллицы и латиницы
    (используется также для коротких строк, на которых langdetect неустойчив).
    """
    sample = clean_whitespace(text)[:1000]
    if len(sample) < 3:
        return UNKNOWN_LANGUAGE
    if _LANGDETECT_AVAILABLE and len(sample) >= 20:
        try:
            code = str(detect(sample)).lower().split("-")[0]
        except LangDetectException:
            code = ""
        if _LANGUAGE_CODE_RE.match(code):
            return code
    return _detect_by_script(sample)


def _detect_by_script(sample: str) -> str:
    """Резервное определение языка по преобладающему алфавиту."""
    cyrillic = len(_CYRILLIC_RE.findall(sample))
    latin = len(_LATIN_RE.findall(sample))
    if cyrillic == 0 and latin == 0:
        return UNKNOWN_LANGUAGE
    if cyrillic >= latin:
        return "ru"
    return "en"


def normalize_language_code(code: str | None) -> str:
    """Приводит код языка источника к ISO 639-1 или возвращает пустую строку, если он непригоден."""
    if not code:
        return ""
    normalized = code.strip().lower().split("-")[0]
    return normalized if _LANGUAGE_CODE_RE.match(normalized) else ""


def normalize_doi(value: str | None) -> str | None:
    """DOI без префикса `https://doi.org/`, в нижнем регистре; None, если значение не похоже на DOI."""
    if not value:
        return None
    candidate = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        candidate = candidate.removeprefix(prefix)
    candidate = candidate.strip()
    return candidate if candidate.startswith("10.") and "/" in candidate else None


def normalize_encyclopedia_title(title: str) -> str:
    """Ключ кеша энциклопедии: trim + нижний регистр + схлопнутые пробелы."""
    return _WHITESPACE_RE.sub(" ", title.strip()).lower()


def tokenize(value: str) -> list[str]:
    """Токены слова в нижнем регистре (для оценки релевантности RSS-записей)."""
    return _TOKEN_RE.findall(normalize_text(value))


def term_coverage(term: str, text: str) -> float:
    """Доля токенов поисковой фразы, встречающихся в тексте (§6 HANDOFF: порог 0.5 для RSS)."""
    term_tokens = set(tokenize(term))
    if not term_tokens:
        return 0.0
    text_tokens = set(tokenize(text))
    return len(term_tokens & text_tokens) / len(term_tokens)


def sanitize_raw_meta(raw: dict[str, Any], allowed_keys: frozenset[str]) -> dict[str, Any]:
    """Оставляет только разрешённые адаптером ключи и гарантирует размер ≤ 8 КБ (J-1, §17 HANDOFF)."""
    filtered: dict[str, Any] = {}
    for key in sorted(allowed_keys & raw.keys()):
        value = raw[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            filtered[key] = truncate(value, 512) if isinstance(value, str) else value
        elif isinstance(value, (list, tuple)):
            filtered[key] = [truncate(str(item), 256) for item in list(value)[:10]]
        elif isinstance(value, dict):
            filtered[key] = {str(k): truncate(str(v), 256) for k, v in list(value.items())[:10]}
    while filtered and _json_size(filtered) > MAX_RAW_META_BYTES:
        filtered.pop(max(filtered, key=lambda k: _json_size({k: filtered[k]})))
    return filtered


def _json_size(value: dict[str, Any]) -> int:
    """Размер JSON-представления в байтах (приближение к `pg_column_size(jsonb)`)."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def encode_page_token(relevance_rank: int, document_id: str) -> str:
    """Непрозрачный keyset-токен `base64("<rank>:<document_id>")` (§10.1)."""
    return base64.urlsafe_b64encode(f"{relevance_rank}:{document_id}".encode()).decode("ascii")


def decode_page_token(token: str) -> tuple[int, str]:
    """Разбор keyset-токена; ValueError при подделке или повреждении."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("page_token повреждён") from exc
    rank, separator, document_id = raw.partition(":")
    if not separator or not rank.isdigit() or len(document_id) != 36:
        raise ValueError("page_token имеет неверный формат")
    return int(rank), document_id
