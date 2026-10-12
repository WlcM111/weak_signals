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

EXPAND_MAX_TOKENS = 1200  # фразы темы и до 40 технологий
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


MAX_TECHNOLOGIES = 40          # в работу идут первые 40 после очистки — бюджет прицельного прохода
MAX_TECHNOLOGIES_RAW = 80      # сколько принимается из ответа модели (схема ответа)
MIN_TECHNOLOGY_LENGTH = 2
MAX_TECHNOLOGY_LENGTH = 80
MAX_TECHNOLOGY_WORDS = 6
# Общие области — не технологии: по ним прицельный поиск возвращает массовый поток, а не ранние сигналы.
GENERIC_TECHNOLOGIES = frozenset({
    "ai", "artificial intelligence", "machine learning", "deep learning", "neural networks", "generative ai",
    "large language models", "llm", "llms", "edge computing", "cloud computing", "cybersecurity", "robotics",
    "fintech", "blockchain", "internet of things", "iot", "data center", "data centers", "big data", "automation",
    "digital transformation", "industry 4.0", "computer vision", "natural language processing", "5g",
})


# Этап 4.2 (замер 07.10.2026: у 44 из 226 названий в конце слово-категория, много зрелых учебных тем).
CATEGORY_TAILS = frozenset("""algorithm algorithms system systems model models framework frameworks technique techniques
method methods mechanism mechanisms solution solutions tool tools platform platforms approach approaches application
applications strategy strategies technology technologies process processes architecture architectures engine engines
interface interfaces capability capabilities service services analytics management optimization optimisation
integration automation""".split())
MATURE_TECHNOLOGIES = frozenset({
    "adversarial training", "differential privacy", "federated learning", "anomaly detection", "predictive maintenance",
    "digital twin", "digital twins", "digital twin simulation", "reinforcement learning", "transfer learning",
    "robotic process automation", "smart contracts", "explainable ai", "machine vision", "swarm robotics",
    "quantum computing", "zero trust", "biometric authentication", "chatbots", "recommendation systems",
    "generative design", "collaborative robots", "cobots", "autonomous vehicles", "augmented reality",
    "virtual reality", "3d printing", "additive manufacturing", "model compression", "knowledge distillation",
    "neural network pruning", "microservices", "data lakes", "wearables",
})
MARKETING_PREFIXES = ("ai-powered", "ai-driven", "ai-based", "ai-enabled", "intelligent", "smart", "advanced",
                      "next-generation", "next generation", "innovative", "cognitive")


_KEY_SPLIT = (" for ", " in ", " on ", " with ", " using ", " via ", " based ", " of ")
_KEY_WORDS = frozenset({"for", "in", "on", "with", "using", "via", "based", "of", "and", "the"})
# Этап 5.0 (F18): отраслевые уточнения — конкретное применение, не общее слово.
_GENERIC_QUALIFIERS = frozenset("""systems system applications application business businesses enterprises enterprise
organizations data operations solutions platforms decisions decision""".split())


def mature_key(term: str) -> str:
    """Ключевая часть названия до уточнения (этап 4.3): «federated learning for factories» → «federated learning»."""
    key = " ".join(term.lower().split())
    for marker in _KEY_SPLIT:
        if marker in f" {key} ":
            key = f" {key} ".split(marker, 1)[0].strip()
    return key


def is_specific_technology(term: str) -> bool:
    """Название конкретной технологии: не кончается словом-категорией, не зрелая тема и не её вариант с
    уточнением («federated learning for …»), без рекламного определения."""
    key = term.lower()
    words = key.split()
    if not words or words[-1] in CATEGORY_TAILS or key in MATURE_TECHNOLOGIES:
        return False
    if mature_key(key) in MATURE_TECHNOLOGIES:
        # этап 4.6 (аудит P17): вариант зрелого метода отсекается, только если уточнение общее («… for systems»);
        # конкретное применение («federated learning for anti-money laundering») — кандидат
        qualifier = [w for w in key[len(mature_key(key)):].split() if w not in _KEY_WORDS]
        if all(w in _GENERIC_QUALIFIERS for w in qualifier):
            return False
    return not key.startswith(MARKETING_PREFIXES)


def clean_technologies(items: Sequence[str], forbidden: Sequence[str] = ()) -> tuple[str, ...]:
    """Технологии из ответа модели: длина, не больше 6 слов, конкретные (этап 4.2), без повторов, не больше 40."""
    blocked = {item.lower() for item in forbidden} | GENERIC_TECHNOLOGIES
    seen: dict[str, str] = {}
    for raw in items:
        term = " ".join(str(raw).split()).strip(" .,;:")
        key = term.lower()
        if not MIN_TECHNOLOGY_LENGTH <= len(term) <= MAX_TECHNOLOGY_LENGTH or len(term.split()) > MAX_TECHNOLOGY_WORDS:
            continue
        if key in blocked or key in seen or not is_specific_technology(term):
            continue
        seen[key] = term
    return tuple(seen.values())[:MAX_TECHNOLOGIES]


def technologies_payload(value: object) -> tuple[list[str], int, str | None]:
    """Отдельная проверка списка технологий из ответа модели: строки, не больше 80 (лишние отбрасываются).

    Возвращает принятые строки, сколько элементов вернула модель и описание нарушения (None — без нарушений).
    Ошибка в этом списке не отменяет фразы темы: расширение остаётся, технологий будет меньше или ни одной.
    """
    if value is None:
        return [], 0, None
    if not isinstance(value, list):
        return [], 0, f"technologies — не список, а {type(value).__name__}"
    strings = [item for item in value if isinstance(item, str)]
    problems = []
    if len(strings) != len(value):
        problems.append(f"не строк: {len(value) - len(strings)}")
    if len(strings) > MAX_TECHNOLOGIES_RAW:
        problems.append(f"элементов {len(value)} больше {MAX_TECHNOLOGIES_RAW}, взяты первые {MAX_TECHNOLOGIES_RAW}")
    return strings[:MAX_TECHNOLOGIES_RAW], len(value), "; ".join(problems) or None


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
        cached = await self._expansions.get(query_norm, EXPAND_PROMPT_VERSION)
        if cached is not None:
            log.info("expand.result", cached=True, used_fallback=cached.used_fallback)
            return cached
        expansion = await self._generate(query_text, query_norm)
        # Резервное расширение не кешируется: временный сбой не закрепляется для этого запроса навсегда.
        if not expansion.used_fallback:
            await self._expansions.save(expansion, EXPAND_PROMPT_VERSION)
        log.info(
            "expand.result",
            cached=False,
            used_fallback=expansion.used_fallback,
            ru_count=len(expansion.ru_terms),
            en_count=len(expansion.en_terms),
            technologies=len(expansion.technologies),
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
            log.warning("expand.fallback", reason=type(error).__name__, detail=str(error)[:300])
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
        raw, returned, problem = technologies_payload(payload.get("technologies"))
        technologies = clean_technologies(raw, forbidden=(*DOMAIN_TAGS, *en_terms))
        (log.warning if problem else log.info)(
            "expand.technologies", returned=returned, accepted=len(raw), kept=len(technologies), problem=problem
        )
        return QueryExpansion(
            query_norm=query_norm,
            ru_terms=ru_terms,
            en_terms=en_terms,
            domain_tags=tags,
            technologies=technologies,
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
