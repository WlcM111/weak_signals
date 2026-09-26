"""Пакетная доводка показанных карточек (режим отбора rubric): один структурированный ответ на пачку карточек.

Карточка по-прежнему строится только по её источникам: числа в фактических полях обязаны встречаться
в названиях или фрагментах источников, компании — упоминаться в них дословно (прочие отбрасываются),
кейс-пример — ссылаться на документ карточки, тексты — быть русскими. Карточка, не прошедшая проверку,
в ответ не попадает: вызывающая сторона строит её прежним способом. Так пакетный вызов не может ухудшить
выдачу — он только заменяет экстрактивные заготовки и шаблонные названия там, где модель ответила корректно.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from insight.application.json_output import OutputRejected, parse_and_validate
from insight.application.prompt_builder import FINALIZE_PROMPT_VERSION, PromptBuilder
from insight.application.use_cases.rubric_judge import RubricSource
from insight.domain import grounding
from insight.domain.errors import ProviderError
from insight.domain.values import Purpose
from ws_common.logging import get_logger

FINALIZE_BATCH = 5
FINALIZE_MAX_TOKENS = 4000
MAX_CARDS = 15
MAX_SOURCES = 6
MAX_TEXT = 900
MIN_CYRILLIC = 0.5
MAX_COMPANIES = 5

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
    """Карточка, прошедшая проверку по источникам."""

    candidate_id: str
    title_ru: str
    description_ru: str
    advantage_ru: str
    case_example_ru: str
    case_document_id: str
    why_ru: str
    companies: tuple[str, ...]
    stage: int
    trend: int
    stage_reason_ru: str
    trend_reason_ru: str
    source_summaries: tuple[tuple[str, str, str], ...] = ()  # (document_id, summary_ru, kind)


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
        "stage": card.stage or None,
        "trend": card.trend or None,
        "assessment": card.judge_reason_ru[:300],
        "sources": [
            {
                "id": f"d{number}",
                "title": source.title[:200],
                "site": source.domain,
                "type": source.source_type,
                "date": source.published,
                "lang": source.language_code,
                "text": source.snippet[:MAX_TEXT],
            }
            for number, source in enumerate(card.sources[:MAX_SOURCES], 1)
        ],
    }


def _supported_numbers(sources: Sequence[RubricSource]) -> set[float]:
    found: set[float] = set()
    for source in sources:
        for match in grounding.NUMBER_RE.finditer(f"{source.title} {source.snippet}"):
            value = grounding.normalize_number(match.group(0))
            if value is not None:
                found.update({value, round(value, 2)})
    return found


def check_card(card: CardInput, row: dict) -> tuple[FinalizedCard | None, str]:
    """Проверяет ответ модели по одной карточке; возвращает карточку или причину отказа."""
    sources = card.sources[:MAX_SOURCES]
    doc_ids = {f"d{number}": source.document_id for number, source in enumerate(sources, 1)}
    clean = {key: grounding.strip_doc_refs(str(row[key])) for key in
             ("title_ru", "description_ru", "advantage_ru", "case_example_ru", "why_ru",
              "stage_reason_ru", "trend_reason_ru")}
    supported = _supported_numbers(sources)
    factual = " ".join(clean[key] for key in ("title_ru", "description_ru", "advantage_ru", "case_example_ru", "why_ru"))
    bad = [value for value in grounding.extract_numbers(factual)
           if value not in supported and round(value, 2) not in supported]
    if bad:
        return None, "числа, отсутствующие в источниках: " + ", ".join(f"{value:g}" for value in bad[:5])
    for key in ("description_ru", "advantage_ru", "why_ru"):
        if grounding.cyrillic_share(clean[key]) < MIN_CYRILLIC:
            return None, f"{key}: текст не на русском языке"
    if not clean["title_ru"]:
        return None, "пустое название"
    corpus = " ".join(f"{source.title} {source.snippet}" for source in sources).casefold()
    companies: list[str] = []
    for name in row.get("companies", []):
        name = " ".join(str(name).split())
        if len(name) >= 2 and name.casefold() in corpus and name not in companies:
            companies.append(name)
    summaries: list[tuple[str, str, str]] = []
    by_id = {source.document_id: source for source in sources}
    for item in row.get("source_summaries", []):
        document_id = doc_ids.get(str(item.get("document_id", "")))
        text = grounding.strip_doc_refs(str(item.get("summary_ru", "")))[:400]
        if document_id is None or not text or any(existing[0] == document_id for existing in summaries):
            continue
        russian = by_id[document_id].language_code == "ru"
        summaries.append((document_id, text, "ORIGINAL_RU" if russian else "GENERATIVE_SUMMARY"))
    return FinalizedCard(
        candidate_id=card.candidate_id,
        title_ru=clean["title_ru"][:200],
        description_ru=clean["description_ru"],
        advantage_ru=clean["advantage_ru"],
        case_example_ru=clean["case_example_ru"],
        case_document_id=doc_ids.get(str(row.get("case_document_id", "")), ""),
        why_ru=clean["why_ru"],
        companies=tuple(companies[:MAX_COMPANIES]),
        stage=int(row["stage"]),
        trend=int(row["trend"]),
        stage_reason_ru=clean["stage_reason_ru"],
        trend_reason_ru=clean["trend_reason_ru"],
        source_summaries=tuple(summaries),
    ), ""


class FinalizeCards:
    """Сценарий пакетной доводки карточек."""

    def __init__(self, chain, prompts: PromptBuilder, batch_size: int = FINALIZE_BATCH) -> None:  # noqa: ANN001
        self._chain = chain
        self._prompts = prompts
        self._batch = max(1, min(batch_size, 8))

    async def execute(self, query_text: str, cards: Sequence[CardInput]) -> FinalizeOutcome:
        """Доводит до MAX_CARDS карточек пачками; непрошедшие проверку перечисляются в `rejected`."""
        cards = list(cards)[:MAX_CARDS]
        outcome = FinalizeOutcome()
        if not cards or self._chain.is_empty:
            outcome.rejected = {card.candidate_id: "модель недоступна" for card in cards}
            return outcome
        for start in range(0, len(cards), self._batch):
            chunk = cards[start : start + self._batch]
            short_ids = {f"k{number}": card for number, card in enumerate(chunk, 1)}
            bundle = self._prompts.build_finalize_prompt(
                query_text, [card_payload(short, card) for short, card in short_ids.items()]
            )
            try:
                response = await self._chain.complete(
                    bundle.messages,
                    json_schema=bundle.json_schema,
                    max_tokens=FINALIZE_MAX_TOKENS,
                    purpose=Purpose.INSIGHT,
                    prompt_version=bundle.prompt_version,
                    request_sha256=bundle.request_sha256,
                )
                payload = parse_and_validate(response.result.text, bundle.json_schema)
            except (ProviderError, OutputRejected) as error:
                for card in chunk:
                    outcome.rejected[card.candidate_id] = f"пачка отклонена: {type(error).__name__}"
                log.warning("finalize.batch_failed", start=start, size=len(chunk), detail=str(error)[:200])
                continue
            outcome.provider, outcome.model = response.provider, str(getattr(response.result, "model", ""))
            answered: set[str] = set()
            for row in payload.get("cards", []):
                card = short_ids.get(str(row.get("candidate_id", "")))
                if card is None or card.candidate_id in answered:
                    continue
                answered.add(card.candidate_id)
                finalized, reason = check_card(card, row)
                if finalized is None:
                    outcome.rejected[card.candidate_id] = reason
                else:
                    outcome.cards.append(finalized)
            for card in chunk:
                if card.candidate_id not in answered:
                    outcome.rejected[card.candidate_id] = "нет в ответе модели"
        log.info("finalize.done", requested=len(cards), accepted=len(outcome.cards), rejected=len(outcome.rejected),
                 provider=outcome.provider)
        return outcome
