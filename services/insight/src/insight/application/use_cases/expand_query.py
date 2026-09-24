"""Сценарий ExpandQuery: запрос пользователя → поисковые фразы ru/en (§7 HANDOFF)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import yaml

from insight.application.dto import LLMResult
from insight.application.json_output import OutputRejected, parse_and_validate
from insight.application.ports import ExpansionRepository, MetricsSink, NullMetrics
from insight.application.prompt_builder import EXPAND_PROMPT_VERSION, PromptBuilder
from insight.application.provider_chain import ProviderChain
from insight.domain.entities import QueryExpansion
from insight.domain.errors import ProviderError
from ws_common.query_fallback import fallback_terms
from insight.domain.values import (
    DOMAIN_TAGS,
    MAX_DOMAIN_TAGS,
    MAX_TERM_LENGTH,
    MAX_TERMS,
    MIN_TERM_LENGTH,
    ProviderName,
    Provenance,
    Purpose,
)
from ws_common.logging import get_logger

EXPAND_MAX_TOKENS = 512
MIN_STEM_LENGTH = 6
_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)
log = get_logger("insight.expand_query")


def normalize(query_text: str) -> str:
    """Нормализация запроса: нижний регистр, обрезка, схлопывание пробелов."""
    return " ".join(query_text.lower().split())


class Glossary:
    """Словарь ru→en и ключевые слова областей для резервного расширения без LLM."""

    def __init__(self, terms: dict[str, str], domain_keywords: dict[str, list[str]]) -> None:
        self._terms = {key.lower(): value for key, value in terms.items()}
        self._domains = {
            domain: [word.lower() for word in words] for domain, words in domain_keywords.items()
        }

    @staticmethod
    def load(path: Path) -> Glossary:
        """Читает `glossary_ru_en.yaml`; отсутствие файла не мешает запуску сервиса."""
        file = Path(path)
        if not file.is_file():
            log.warning("glossary.missing", path=str(file))
            return Glossary({}, {})
        payload = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        return Glossary(payload.get("terms", {}) or {}, payload.get("domain_keywords", {}) or {})

    @property
    def terms(self) -> dict[str, str]:
        """Словарь ru→en (ключи в нижнем регистре)."""
        return dict(self._terms)

    def matches(self, query_norm: str) -> list[str]:
        """Термины глоссария, встречающиеся в запросе (для русских фраз)."""
        return [term for term in self._terms if _contains(query_norm, term)]

    def translate(self, query_norm: str) -> str:
        """Пословный перевод запроса; непереведённые слова остаются как есть."""
        if query_norm in self._terms:
            return self._terms[query_norm]
        if not self._terms:
            return query_norm
        return " ".join(self._terms.get(word, word) for word in _WORD_RE.findall(query_norm))

    def domain_tags(self, query_norm: str) -> list[str]:
        """Метки областей по ключевым словам глоссария."""
        found = [
            domain
            for domain, words in self._domains.items()
            if any(_contains(query_norm, word) for word in words)
        ]
        return found[:MAX_DOMAIN_TAGS]


def _contains(query_norm: str, term: str) -> bool:
    """Встречается ли термин в запросе с учётом словоформ.

    Морфологического анализатора в сервисе нет, поэтому длинные слова сравниваются по основе:
    «кибербезопасность» находится и в запросе «слабые сигналы в кибербезопасности».
    """
    if len(term) < MIN_STEM_LENGTH or " " in term:
        return term in query_norm
    return term[:MIN_STEM_LENGTH] in query_norm


def clean_terms(terms: Sequence[str], forbidden: Sequence[str] = ()) -> tuple[str, ...]:
    """Пост-обработка фраз: длины, дедупликация, приоритет словосочетаниям (§7 HANDOFF)."""
    blocked = {item.lower() for item in forbidden}
    seen: dict[str, str] = {}
    for raw in terms:
        term = " ".join(str(raw).split())
        key = term.lower()
        if not MIN_TERM_LENGTH <= len(term) <= MAX_TERM_LENGTH or key in blocked or key in seen:
            continue
        seen[key] = term
    ordered = sorted(seen.values(), key=lambda term: (len(term.split()) < 2, len(term)))
    return tuple(ordered[:MAX_TERMS])


class ExpandQuery:
    """Расширяет запрос через LLM, с кешем и резервным путём по глоссарию."""

    def __init__(
        self,
        chain: ProviderChain,
        prompts: PromptBuilder,
        expansions: ExpansionRepository,
        glossary: Glossary,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._chain = chain
        self._prompts = prompts
        self._expansions = expansions
        self._glossary = glossary
        self._metrics: MetricsSink = metrics or NullMetrics()

    async def execute(self, query_text: str) -> QueryExpansion:
        """Кеш → LLM → резерв; ответ всегда содержит хотя бы по одной фразе на язык."""
        query_norm = normalize(query_text)
        cached = await self._expansions.get(query_norm)
        if cached is not None:
            log.info("expand.result", cached=True, used_fallback=cached.used_fallback)
            return cached
        expansion = await self._generate(query_text, query_norm)
        await self._expansions.save(expansion, EXPAND_PROMPT_VERSION)
        log.info(
            "expand.result",
            cached=False,
            used_fallback=expansion.used_fallback,
            ru_count=len(expansion.ru_terms),
            en_count=len(expansion.en_terms),
        )
        return expansion

    async def _generate(self, query_text: str, query_norm: str) -> QueryExpansion:
        """Вызов модели; любая ошибка или невалидный ответ приводят к резервному расширению."""
        if self._chain.is_empty:
            return self.fallback(query_text, query_norm)
        bundle = self._prompts.build_expand_prompt(query_text, DOMAIN_TAGS)
        try:
            outcome = await self._chain.complete(
                bundle.messages,
                json_schema=bundle.json_schema,
                max_tokens=EXPAND_MAX_TOKENS,
                purpose=Purpose.EXPAND,
                prompt_version=bundle.prompt_version,
                request_sha256=bundle.request_sha256,
            )
            payload = parse_and_validate(outcome.result.text, bundle.json_schema)
        except (ProviderError, OutputRejected) as error:
            log.warning("expand.fallback", reason=type(error).__name__)
            return self.fallback(query_text, query_norm)
        return self._from_payload(query_norm, payload, outcome.result, outcome.provider)

    def _from_payload(
        self, query_norm: str, payload: dict, result: LLMResult, provider: str
    ) -> QueryExpansion:
        """Собирает расширение из ответа модели с пост-обработкой фраз."""
        ru_terms = clean_terms(payload.get("ru_terms", []), forbidden=DOMAIN_TAGS)
        en_terms = clean_terms(payload.get("en_terms", []), forbidden=DOMAIN_TAGS)
        if not ru_terms or not en_terms:
            return self.fallback(query_norm, query_norm)
        tags = tuple(
            tag for tag in dict.fromkeys(payload.get("domain_tags", [])) if tag in DOMAIN_TAGS
        )[:MAX_DOMAIN_TAGS]
        return QueryExpansion(
            query_norm=query_norm,
            ru_terms=ru_terms,
            en_terms=en_terms,
            domain_tags=tags,
            used_fallback=False,
            provenance=Provenance(
                provider=provider,
                model=result.model,
                prompt_version=EXPAND_PROMPT_VERSION,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                attempts=1,
                latency_ms=result.latency_ms,
            ),
        )

    def fallback(self, query_text: str, query_norm: str) -> QueryExpansion:
        """Резерв без LLM: сам запрос, совпадения глоссария и его перевод."""
        # содержательные слова запроса без «рамки» («слабые сигналы в области …»), перевод с учётом
        # словоформ; непереведённая кириллица в английские фразы не попадает
        ru_base, en_base = fallback_terms(query_text, self._glossary.terms)
        ru_terms = clean_terms([*ru_base, *self._glossary.matches(query_norm)])
        en_terms = clean_terms(en_base) or (query_text,)
        return QueryExpansion(
            query_norm=query_norm,
            ru_terms=ru_terms or (query_text,),
            en_terms=en_terms,
            domain_tags=tuple(self._glossary.domain_tags(query_norm)),
            used_fallback=True,
            provenance=Provenance(
                provider=ProviderName.NONE.value, prompt_version=EXPAND_PROMPT_VERSION
            ),
        )
