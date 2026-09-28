"""Пакетная доводка показанных карточек (режим rubric), версия 2: все поля заполнены, все тексты на русском.

Прогон 26.09.2026: доводка приняла 23 карточки из ~77, у остальных на экране осталась английская аннотация
и «не сформулировано автоматически». Причины в коде версии 1: пачка из пяти карточек с резюме источников не
помещалась в ответ; проверка чисел ловила любые цифры («6G», «H100», годы) и сверяла их только с первыми
900 символами источника; компании сравнивались без нормализации; одна ошибка отклоняла всю пачку.

Версия 2: пачки по две карточки, неудачная пачка и отклонённая карточка повторяются по одной; ответ
разбирается по карточкам; число, которого нет в источниках, удаляет своё предложение, а не карточку; названия
компаний сравниваются после нормализации; резюме на русском требуется для каждого источника карточки.
Карточка принимается, только если заполнены все текстовые поля и все они на русском языке.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from insight.application.json_output import OutputRejected, parse
from insight.application.prompt_builder import FINALIZE_PROMPT_VERSION, PromptBuilder
from insight.application.use_cases.rubric_judge import RubricSource
from insight.domain import grounding
from insight.domain.errors import ProviderError
from insight.domain.values import Purpose
from ws_common.logging import get_logger

# Одна карточка на вызов: v6 при пачках по 2 дал 51 отказ разбора JSON (длинный ответ обрывался).
FINALIZE_BATCH = 1
FINALIZE_MAX_TOKENS = 4000
MAX_CARDS = 30
MAX_SOURCES = 5
MAX_TEXT = 1500
MIN_CYRILLIC = 0.5
MIN_CYRILLIC_TITLE = 0.3
MIN_TEXT = 10
MAX_COMPANIES = 5
TEXT_FIELDS = ("title_ru", "description_ru", "advantage_ru", "case_example_ru", "why_ru", "stage_reason_ru",
               "trend_reason_ru")
LIMITS = {"title_ru": 200, "description_ru": 1200, "advantage_ru": 600, "case_example_ru": 800, "why_ru": 800,
          "stage_reason_ru": 300, "trend_reason_ru": 300}
STANDALONE_NUMBER_RE = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)?(?![\w])")
SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
_QUOTES_RE = re.compile(r"[«»\"'“”„()\[\]]")
TERM_RE = re.compile(r"\(([^()]{2,80})\)")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")
# Общие слова английских заголовков не доказывают, что источник о той же технологии.
GENERIC_WORDS = frozenset({
    "with", "from", "that", "this", "using", "based", "towards", "toward", "into", "over", "under", "their", "which",
    "through", "approach", "approaches", "framework", "frameworks", "method", "methods", "system", "systems", "model",
    "models", "learning", "network", "networks", "analysis", "study", "review", "survey", "data", "deep", "neural",
    "novel", "efficient", "robust", "real", "time", "driven", "aware", "large", "language", "artificial", "intelligence",
    "machine", "robot", "robots", "robotic", "robotics", "human", "scale", "scalable", "multi", "design", "control",
    "performance", "evaluation", "enabling", "enabled", "application", "applications", "case", "technology",
    "technologies", "platform", "platforms", "solution", "solutions", "systematic", "empirical", "toward", "task",
})
TERM_WORD_SHARE = 0.6
MAX_TERMS = 12


def significant_words(text: str) -> set[str]:
    """Значимые латинские слова: строчные, не короче 4 букв, без общих слов."""
    words = set()
    for token in _LATIN_RE.findall(text or ""):
        for part in token.lower().split("-"):
            if len(part) >= 4 and part not in GENERIC_WORDS:
                words.add(part)
    return words


def candidate_terms(sources: Sequence[RubricSource]) -> list[str]:
    """Допустимые термины для названия: имя до двоеточия в заголовке и собственные имена/аббревиатуры (ForeTac-VLA, OCS)."""
    terms: list[str] = []
    for source in sources:
        title = source.title or ""
        head = title.split(":", 1)[0].strip()
        if ":" in title and 2 <= len(head) <= 60:
            terms.append(head)
        terms += [t for t in _LATIN_RE.findall(title) if len(t) >= 3 and (any(c.isupper() for c in t[1:]) or "-" in t)]
    return list(dict.fromkeys(terms))[:MAX_TERMS]


def source_matches(source: RubricSource, anchor: set[str]) -> bool:
    """Источник о той же технологии: есть общее значимое слово с термином. Источник без латинских слов (русский
    текст) лексически не сравнить — он остаётся."""
    words = significant_words(f"{source.title} {source.snippet[:400]}")
    return not words or bool(words & anchor)


def term_supported(term: str, corpus: str, corpus_words: set[str]) -> bool:
    """Термин подтверждён: дословно есть в источниках или в них есть не менее 60 % его значимых слов."""
    if normalize_name(term) and normalize_name(term) in corpus:
        return True
    words = significant_words(term)
    return bool(words) and len(words & corpus_words) / len(words) >= TERM_WORD_SHARE

log = get_logger("insight.finalize")


@dataclass(frozen=True, slots=True)
class CardInput:
    """Показанная карточка: кандидат, его источники и стадия/тренд рубричной оценки."""

    candidate_id: str
    title_auto: str
    keyphrases: tuple[str, ...] = ()
    sources: tuple[RubricSource, ...] = ()
    stage: int = 0
    trend: int = 0
    judge_reason_ru: str = ""


@dataclass(frozen=True, slots=True)
class FinalizedCard:
    """Карточка, прошедшая проверку: все поля заполнены, тексты на русском, числа и компании — из источников."""

    candidate_id: str
    title_ru: str
    description_ru: str
    advantage_ru: str
    case_example_ru: str
    case_document_id: str
    why_ru: str
    companies: tuple[str, ...]
    stage_reason_ru: str
    trend_reason_ru: str
    source_summaries: tuple[tuple[str, str, str], ...] = ()  # (document_id, summary_ru, kind)
    stage: int = 0  # стадию и тренд задаёт рубричная оценка; поля оставлены для совместимости контракта
    trend: int = 0


@dataclass(slots=True)
class FinalizeOutcome:
    """Итог доводки: принятые карточки и причины отказа по остальным."""

    cards: list[FinalizedCard] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    prompt_version: str = FINALIZE_PROMPT_VERSION

    @property
    def used_fallback(self) -> bool:
        return not self.cards


def card_payload(short_id: str, card: CardInput) -> dict:
    """Карточка в JSON-вход модели; документы — под короткими номерами d1…dN."""
    return {
        "id": short_id,
        "auto_title": card.title_auto[:200],
        "keyphrases": list(card.keyphrases[:8]),
        "assessment": card.judge_reason_ru[:300],
        "terms": candidate_terms(card.sources[:MAX_SOURCES]),
        "sources": [
            {"id": f"d{number}", "title": source.title[:300], "site": source.domain, "type": source.source_type,
             "date": source.published, "lang": source.language_code, "text": source.snippet[:MAX_TEXT]}
            for number, source in enumerate(card.sources[:MAX_SOURCES], 1)
        ],
    }


def normalize_name(text: str) -> str:
    """Название для сравнения: Unicode NFKC, регистр, без кавычек и скобок, одиночные пробелы."""
    return " ".join(_QUOTES_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).split())


def supported_numbers(sources: Sequence[RubricSource]) -> set[float]:
    """Числа источников: из названий, текстов и годов публикации."""
    found: set[float] = set()
    for source in sources:
        for match in grounding.NUMBER_RE.finditer(f"{source.title} {source.snippet}"):
            value = grounding.normalize_number(match.group(0))
            if value is not None:
                found.update({value, round(value, 2)})
        if source.published[:4].isdigit():
            found.add(float(source.published[:4]))
    return found


def unsupported(text: str, supported: set[float]) -> list[float]:
    """Отдельно стоящие числа текста, которых нет в источниках («6G» и «H100» числами не считаются)."""
    values = [grounding.normalize_number(match.group(0)) for match in STANDALONE_NUMBER_RE.finditer(text)]
    return [value for value in values if value is not None and value not in supported and round(value, 2) not in supported]


def repair_numbers(text: str, supported: set[float]) -> str:
    """Удаляет предложения с числами, которых нет в источниках."""
    return " ".join(part for part in SENTENCE_RE.split(text) if part and not unsupported(part, supported)).strip()


def check_card(card: CardInput, row: object) -> tuple[FinalizedCard | None, str]:
    """Проверяет ответ модели по одной карточке; возвращает карточку или причину отказа."""
    if not isinstance(row, dict):
        return None, "ответ по карточке не объект"
    sources = card.sources[:MAX_SOURCES]
    doc_ids = {f"d{number}": source.document_id for number, source in enumerate(sources, 1)}
    # Карточка строится только по источникам, которые описывают выбранную технологию (27.09: в одну карточку
    # попадали несвязанные заметки о раундах и статьи о других технологиях).
    chosen = list(dict.fromkeys(doc_ids[key] for key in (str(x).strip() for x in row.get("source_ids") or [])
                                if key in doc_ids))
    if not chosen:
        return None, "не выбраны источники, описывающие технологию"
    sources = tuple(source for source in sources if source.document_id in chosen)
    title = " ".join(grounding.strip_doc_refs(str(row.get("title_ru", "") or "")).split())[: LIMITS["title_ru"]]
    terms = [term for term in TERM_RE.findall(title) if normalize_name(term)]
    corpus = normalize_name(" ".join(f"{source.title} {source.snippet}" for source in sources))
    corpus_words = significant_words(" ".join(f"{source.title} {source.snippet}" for source in sources))
    if not any(term_supported(term, corpus, corpus_words) for term in terms):
        return None, "в названии нет оригинального термина из выбранных источников"
    # Источники проверяет код, а не только выбор модели (v6: в 8 карточках из 28 были несвязанные источники):
    # остаются источники с общим значимым словом с термином и латиницей названия.
    anchor = significant_words(" ".join(terms) + " " + title)
    relevant = tuple(s for s in sources if source_matches(s, anchor))
    sources = relevant or sources
    doc_ids = {key: value for key, value in doc_ids.items() if value in {s.document_id for s in sources}}
    corpus = normalize_name(" ".join(f"{source.title} {source.snippet}" for source in sources))
    supported = supported_numbers(sources)
    clean: dict[str, str] = {}
    for key in TEXT_FIELDS:
        text = " ".join(grounding.strip_doc_refs(str(row.get(key, "") or "")).split())[: LIMITS[key]]
        clean[key] = text if key == "title_ru" else repair_numbers(text, supported)
    if unsupported(clean["title_ru"], supported):
        return None, "в названии число, которого нет в источниках"

    for key in TEXT_FIELDS:
        minimum, share = (3, MIN_CYRILLIC_TITLE) if key == "title_ru" else (MIN_TEXT, MIN_CYRILLIC)
        if len(clean[key]) < minimum:
            return None, f"{key}: поле пустое"
        if grounding.cyrillic_share(clean[key]) < share:
            return None, f"{key}: текст не на русском языке"
    companies: list[str] = []
    for name in row.get("companies") or []:
        name = " ".join(str(name).split())
        if len(name) >= 2 and normalize_name(name) and normalize_name(name) in corpus and name not in companies:
            companies.append(name)
    by_id = {source.document_id: source for source in sources}
    summaries: dict[str, tuple[str, str, str]] = {}
    for item in row.get("source_summaries") or []:
        if not isinstance(item, dict):
            continue
        document_id = doc_ids.get(str(item.get("document_id", "")).strip())
        text = repair_numbers(" ".join(grounding.strip_doc_refs(str(item.get("summary_ru", "") or "")).split())[:400],
                              supported)
        if document_id and document_id not in summaries and len(text) >= MIN_TEXT and \
                grounding.cyrillic_share(text) >= MIN_CYRILLIC:
            kind = "ORIGINAL_RU" if by_id[document_id].language_code == "ru" else "GENERATIVE_SUMMARY"
            summaries[document_id] = (document_id, text, kind)
    missing = [source.document_id for source in sources if source.document_id not in summaries]
    if missing:
        return None, f"нет резюме на русском для {len(missing)} источников"
    return FinalizedCard(
        candidate_id=card.candidate_id, title_ru=clean["title_ru"], description_ru=clean["description_ru"],
        advantage_ru=clean["advantage_ru"], case_example_ru=clean["case_example_ru"],
        case_document_id=doc_ids.get(str(row.get("case_document_id", "")).strip(), ""), why_ru=clean["why_ru"],
        companies=tuple(companies[:MAX_COMPANIES]), stage_reason_ru=clean["stage_reason_ru"],
        trend_reason_ru=clean["trend_reason_ru"], source_summaries=tuple(summaries[s.document_id] for s in sources),
    ), ""


class FinalizeCards:
    """Сценарий пакетной доводки карточек."""

    def __init__(self, chain, prompts: PromptBuilder, batch_size: int = FINALIZE_BATCH) -> None:  # noqa: ANN001
        self._chain = chain
        self._prompts = prompts
        self._batch = max(1, min(batch_size, 4))

    async def execute(self, query_text: str, cards: Sequence[CardInput]) -> FinalizeOutcome:
        """Доводит до MAX_CARDS карточек; отклонённые в пачке повторяются по одной, причины — в `rejected`."""
        cards = list(cards)[:MAX_CARDS]
        outcome = FinalizeOutcome()
        if not cards or self._chain.is_empty:
            outcome.rejected = {card.candidate_id: "модель недоступна" for card in cards}
            return outcome
        for start in range(0, len(cards), self._batch):
            chunk = cards[start : start + self._batch]
            await self._chunk(query_text, chunk, outcome)
            if len(chunk) > 1:
                for card in [card for card in chunk if card.candidate_id in outcome.rejected]:
                    await self._chunk(query_text, [card], outcome)
        log.info("finalize.done", requested=len(cards), accepted=len(outcome.cards), rejected=len(outcome.rejected),
                 reasons=sorted(set(outcome.rejected.values()))[:10], provider=outcome.provider)
        return outcome

    async def _chunk(self, query_text: str, chunk: list[CardInput], outcome: FinalizeOutcome) -> None:
        short_ids = {f"k{number}": card for number, card in enumerate(chunk, 1)}
        bundle = self._prompts.build_finalize_prompt(
            query_text, [card_payload(short, card) for short, card in short_ids.items()]
        )
        try:
            response = await self._chain.complete(
                bundle.messages, json_schema=bundle.json_schema, max_tokens=FINALIZE_MAX_TOKENS,
                purpose=Purpose.INSIGHT, prompt_version=bundle.prompt_version, request_sha256=bundle.request_sha256,
            )
            payload = parse(response.result.text)
        except (ProviderError, OutputRejected) as error:
            for card in chunk:
                outcome.rejected[card.candidate_id] = f"пачка отклонена: {type(error).__name__}"
            log.warning("finalize.batch_failed", size=len(chunk), detail=str(error)[:200])
            return
        outcome.provider, outcome.model = response.provider, str(getattr(response.result, "model", ""))
        answered: set[str] = set()
        rows = payload.get("cards") if isinstance(payload.get("cards"), list) else []
        for row in rows:
            card = short_ids.get(str(row.get("candidate_id", "")).strip()) if isinstance(row, dict) else None
            if card is None or card.candidate_id in answered:
                continue
            answered.add(card.candidate_id)
            finalized, reason = check_card(card, row)
            if finalized is None:
                outcome.rejected[card.candidate_id] = reason
                log.warning("finalize.card_rejected", candidate_id=card.candidate_id, reason=reason)
            else:
                outcome.rejected.pop(card.candidate_id, None)
                outcome.cards = [c for c in outcome.cards if c.candidate_id != card.candidate_id] + [finalized]
        for card in chunk:
            if card.candidate_id not in answered:
                outcome.rejected[card.candidate_id] = "нет в ответе модели"
