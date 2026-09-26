"""Рубричная оценка кандидатов (режим отбора rubric): слабый сигнал = тема + конкретность + ранняя стадия + проверяемость.

Модель получает полный контекст каждого кандидата — названия, площадки, типы, доверенность, даты и фрагменты
источников и сводку их состава — и возвращает код рубрики разметки (R или причину отказа), четыре критерия,
стадию 1–4 и тренд 1–3 по шкале организаторов. Кандидаты отправляются пачками по RUBRIC_BATCH: при полном
контексте одна пачка из 40 не помещается в разумный вход. Отказ пачки не срывает остальные: её кандидаты
просто остаются без вердикта. Код R при невыполненном критерии считается противоречивым и заменяется на U.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from insight.application.json_output import OutputRejected, parse_and_validate
from insight.application.prompt_builder import PromptBuilder
from insight.domain.errors import ProviderError
from insight.domain.values import Purpose
from ws_common.logging import get_logger

RUBRIC_BATCH = 8
RUBRIC_MAX_TOKENS = 3000
MAX_ITEMS = 40
MAX_SOURCES = 6
MAX_SNIPPET = 400
MAX_TITLE = 200
CODES = ("R", "N-OFF", "N-GEN", "N-OVR", "N-MAT", "N-NOI", "N-HYP", "N-FUND", "U")
LEGACY_VERDICT = {
    "R": "EMERGING_TECHNOLOGY",
    "N-OFF": "OFF_TOPIC",
    "N-GEN": "GENERIC_CONCEPT",
    "N-OVR": "OVERVIEW",
    "N-MAT": "MATURE_TECHNOLOGY",
    "N-NOI": "NOISE",
    "N-HYP": "NOISE",
    "N-FUND": "NOISE",
    "U": "UNCERTAIN",
}

log = get_logger("insight.rubric")


@dataclass(frozen=True, slots=True)
class RubricSource:
    """Источник кандидата в том виде, в каком его видит модель."""

    document_id: str
    title: str
    source_key: str = ""
    source_type: str = ""
    trust_level: str = ""
    published: str = ""
    language_code: str = ""
    snippet: str = ""
    domain: str = ""


@dataclass(frozen=True, slots=True)
class RubricItem:
    """Кандидат для рубричной оценки."""

    candidate_id: str
    title: str
    keyphrases: tuple[str, ...] = ()
    sources: tuple[RubricSource, ...] = ()
    composition_ru: str = ""


@dataclass(frozen=True, slots=True)
class RubricVerdict:
    """Вердикт по кандидату: код рубрики, критерии, стадия, тренд и уверенность."""

    candidate_id: str
    code: str
    on_topic: bool
    concrete: bool
    early_stage: bool
    verifiable: bool
    stage: int
    trend: int
    relevance: int
    confidence: float
    reason_ru: str

    @property
    def verdict(self) -> str:
        """Вердикт в словаре прежней оценки (judge_v1) — для совместимости журнала и контракта."""
        return LEGACY_VERDICT[self.code]


@dataclass(frozen=True, slots=True)
class RubricOutcome:
    """Итог оценки: вердикты по оценённым кандидатам; `used_fallback` — не оценён ни один."""

    verdicts: tuple[RubricVerdict, ...]
    provider: str = ""
    model: str = ""
    used_fallback: bool = False


def payload_item(short_id: str, item: RubricItem) -> dict:
    """Кандидат в JSON-вход модели: только данные, без служебных идентификаторов документов."""
    return {
        "id": short_id,
        "title": item.title[:MAX_TITLE],
        "keyphrases": list(item.keyphrases[:8]),
        "composition": item.composition_ru[:600],
        "sources": [
            {
                "title": source.title[:MAX_TITLE],
                "site": source.domain,
                "type": source.source_type,
                "trust": source.trust_level,
                "date": source.published,
                "lang": source.language_code,
                "text": source.snippet[:MAX_SNIPPET],
            }
            for source in item.sources[:MAX_SOURCES]
        ],
    }


def to_verdict(candidate_id: str, row: dict) -> RubricVerdict:
    """Строка ответа модели (уже проверенная схемой) → вердикт; R без всех критериев становится U."""
    criteria = (bool(row["on_topic"]), bool(row["concrete"]), bool(row["early_stage"]), bool(row["verifiable"]))
    code = str(row["code"])
    if code == "R" and not all(criteria):
        code = "U"
    return RubricVerdict(
        candidate_id=candidate_id,
        code=code,
        on_topic=criteria[0],
        concrete=criteria[1],
        early_stage=criteria[2],
        verifiable=criteria[3],
        stage=int(row["stage"]),
        trend=int(row["trend"]),
        relevance=int(row["relevance"]),
        confidence=round(float(row["confidence"]), 4),
        reason_ru=str(row["reason_ru"]).strip()[:300],
    )


class RubricJudge:
    """Сценарий рубричной оценки кандидатов пачками."""

    def __init__(self, chain, prompts: PromptBuilder, batch_size: int = RUBRIC_BATCH) -> None:  # noqa: ANN001
        self._chain = chain
        self._prompts = prompts
        self._batch = max(1, min(batch_size, 12))

    async def execute(self, query_text: str, items: Sequence[RubricItem]) -> RubricOutcome:
        """Оценивает до MAX_ITEMS кандидатов; пачка, на которую модель не ответила, остаётся без вердикта."""
        items = list(items)[:MAX_ITEMS]
        if not items or self._chain.is_empty:
            return RubricOutcome((), used_fallback=True)
        verdicts: list[RubricVerdict] = []
        provider = model = ""
        for start in range(0, len(items), self._batch):
            chunk = items[start : start + self._batch]
            short_ids = {f"c{number}": item.candidate_id for number, item in enumerate(chunk, 1)}
            bundle = self._prompts.build_rubric_prompt(
                query_text, [payload_item(short, item) for short, item in zip(short_ids, chunk, strict=True)]
            )
            try:
                outcome = await self._chain.complete(
                    bundle.messages,
                    json_schema=bundle.json_schema,
                    max_tokens=RUBRIC_MAX_TOKENS,
                    purpose=Purpose.JUDGE,
                    prompt_version=bundle.prompt_version,
                    request_sha256=bundle.request_sha256,
                )
                payload = parse_and_validate(outcome.result.text, bundle.json_schema)
            except (ProviderError, OutputRejected) as error:
                log.warning("rubric.batch_failed", start=start, size=len(chunk), reason=type(error).__name__,
                            detail=str(error)[:200])
                continue
            provider, model = outcome.provider, str(getattr(outcome.result, "model", ""))
            seen: set[str] = set()
            for row in payload.get("verdicts", []):
                candidate_id = short_ids.get(str(row.get("candidate_id", "")))
                if candidate_id is None or candidate_id in seen:
                    continue
                seen.add(candidate_id)
                verdicts.append(to_verdict(candidate_id, row))
        log.info("rubric.done", requested=len(items), judged=len(verdicts), provider=provider,
                 accepted=sum(1 for verdict in verdicts if verdict.code == "R"))
        return RubricOutcome(tuple(verdicts), provider, model, used_fallback=not verdicts)
