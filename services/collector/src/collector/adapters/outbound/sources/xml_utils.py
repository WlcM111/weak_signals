"""Безопасный разбор XML внешних источников (§17 HANDOFF: `resolve_entities=False`, без сети и DTD)."""

from __future__ import annotations

import re
from typing import Any

from lxml import etree

from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode

_PARSER_OPTIONS = {
    "resolve_entities": False,  # запрет XXE через сущности
    "no_network": True,
    "load_dtd": False,
    "huge_tree": False,
    "recover": False,
}


_DECLARATION_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)


def parse_xml(source: str, *, what: str) -> Any:
    """Разбирает XML-документ; синтаксическая ошибка → `AdapterFailure(PARSE_ERROR)`.

    Текст уже декодирован HTTP-клиентом, поэтому объявление `<?xml … encoding="…"?>` удаляется:
    иначе lxml прочитал бы байты UTF-8 в кодировке из объявления и исказил не-ASCII символы.
    """
    parser = etree.XMLParser(**_PARSER_OPTIONS)
    try:
        return etree.fromstring(_DECLARATION_RE.sub("", source, count=1).encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError as exc:
        raise AdapterFailure(AdapterErrorCode.PARSE_ERROR.value, f"{what}: некорректный XML ({exc})") from exc


def text_of(element: Any) -> str:
    """Текст элемента без окружающих пробелов; пустая строка, если элемента нет."""
    if element is None:
        return ""
    return " ".join((element.text or "").split())
