"""Стадия 1 — расширение запроса в поисковые термины (§7.1 HANDOFF).

Отказ insight не фатален: собираются резервные термины, а факт подмены попадает в статистику
(`expand_used_fallback`), чтобы в отчёте было видно, что расширение не выполнялось моделью.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from orchestrator.application.dto import ExpansionView
from orchestrator.application.ports import InsightClient
from ws_common.logging import get_logger
from ws_common.query_fallback import fallback_terms

MAX_TERMS = 8
_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)
log = get_logger("orchestrator.stage.expand")


class Glossary:
    """Словарь ru → en для резервного перевода запроса без обращения к LLM."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = {key.lower(): value for key, value in mapping.items()}

    @staticmethod
    def load(path: Path) -> Glossary:
        """Читает `glossary_ru_en.yaml`; отсутствие файла — пустой словарь, а не отказ запуска."""
        file = Path(path)
        if not file.is_file():
            log.warning("glossary.missing", path=str(file))
            return Glossary({})
        payload = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        terms = payload.get("terms", payload)
        return Glossary({str(key): str(value) for key, value in terms.items()})

    @property
    def mapping(self) -> dict[str, str]:
        """Словарь ru→en (ключи в нижнем регистре)."""
        return dict(self._mapping)

    def translate(self, text: str) -> str:
        """Пословный перевод по словарю; непереведённые слова остаются как есть."""
        if not self._mapping:
            return text
        phrase = text.lower().strip()
        if phrase in self._mapping:
            return self._mapping[phrase]
        words = _WORD_RE.findall(phrase)
        translated = [self._mapping.get(word, word) for word in words]
        return " ".join(translated)


def fallback_expansion(query_text: str, glossary: Glossary) -> ExpansionView:
    """Резервные термины: сам запрос на русском и его перевод по словарю на английском."""
    ru_terms, en_terms = fallback_terms(query_text, glossary.mapping)
    return ExpansionView(
        ru_terms=ru_terms[:MAX_TERMS],
        en_terms=en_terms[:MAX_TERMS],
        domain_tags=(),
        used_fallback=True,
    )


async def run_expand(insight: InsightClient | None, query_text: str, glossary: Glossary) -> ExpansionView:
    """Вызывает `ExpandQuery`; при любой ошибке возвращает резервные термины."""
    if insight is None:
        log.info("stage.expand", used_fallback=True, reason="insight_disabled")
        return fallback_expansion(query_text, glossary)
    try:
        expansion = await insight.expand_query(query_text)
    except Exception as error:  # noqa: BLE001 - расширение не критично для выполнения задания
        log.warning("stage.expand", used_fallback=True, error=str(error))
        return fallback_expansion(query_text, glossary)
    ru_terms = tuple(expansion.ru_terms[:MAX_TERMS]) or (query_text,)
    en_terms = tuple(expansion.en_terms[:MAX_TERMS]) or (glossary.translate(query_text),)
    log.info(
        "stage.expand",
        used_fallback=expansion.used_fallback,
        ru_terms=len(ru_terms),
        en_terms=len(en_terms),
    )
    return ExpansionView(
        ru_terms=ru_terms,
        en_terms=en_terms,
        domain_tags=tuple(expansion.domain_tags),
        used_fallback=expansion.used_fallback,
    )
