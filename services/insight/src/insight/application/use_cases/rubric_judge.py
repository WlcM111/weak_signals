"""Рубричная оценка кандидатов (режим отбора rubric): слабый сигнал = тема + конкретность + ранняя стадия + проверяемость.

Модель получает полный контекст каждого кандидата — названия, площадки, типы, доверенность, даты и фрагменты
источников и сводку их состава — и возвращает код рубрики разметки (R или причину отказа), четыре критерия,
стадию 1–4 и тренд 1–3 по шкале организаторов. Кандидаты отправляются пачками по RUBRIC_BATCH: при полном
контексте одна пачка из 40 не помещается в разумный вход. Отказ пачки не срывает остальные: её кандидаты
просто остаются без вердикта. Код R при невыполненном критерии считается противоречивым и заменяется на U.

Устойчивость (прогон 26.09: на B v2 без вердикта 79 строк из 268): ответ разбирается по каждому вердикту
отдельно — типы приводятся (строка «2» → 2, «true» → true), лишние поля отбрасываются, длинное обоснование
обрезается; неверный вердикт выпадает один, а не вся пачка. Пачка, на которую модель не ответила разборчиво,
повторяется по одному кандидату.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from insight.application.json_output import OutputRejected, parse, validate
from insight.application.prompt_builder import PromptBuilder
from insight.application.use_cases.maturity_check import (
    NOVELTY_RU,
    MaturityCheck,
    MaturityInput,
    MaturityRule,
    research_only,
)
from insight.domain.errors import ProviderError
from insight.domain.values import Purpose
from ws_common.logging import get_logger

RUBRIC_BATCH = 4
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
    technology_ru: str = ""
    profile_ru: str = ""

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


def _int(value: object, low: int, high: int) -> int | None:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return min(max(number, low), high)


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    return True if text in ("true", "да", "1", "yes") else False if text in ("false", "нет", "0", "no") else None


def coerce_verdict(row: object) -> dict | None:
    """Приводит строку ответа к схеме: типы, границы, длина обоснования; None — если строку не спасти."""
    if not isinstance(row, dict):
        return None
    out = {"candidate_id": str(row.get("candidate_id", "")).strip(),
           "code": str(row.get("code", "")).strip().upper(),
           "stage": _int(row.get("stage"), 1, 4), "trend": _int(row.get("trend"), 1, 3),
           "relevance": _int(row.get("relevance"), 0, 3),
           "reason_ru": " ".join(str(row.get("reason_ru", "")).split())[:300]}
    for key in ("on_topic", "concrete", "early_stage", "verifiable"):
        out[key] = _bool(row.get(key))
    try:
        out["confidence"] = min(max(float(str(row.get("confidence")).strip()), 0.0), 1.0)
    except (TypeError, ValueError):
        out["confidence"] = None
    if any(value is None for value in out.values()):
        return None
    # Извлечённая сущность и профиль — данные для локального классификатора; без них вердикт остаётся годным.
    out["technology_ru"] = " ".join(str(row.get("technology_ru", "") or "").split())[:200]
    out["profile_ru"] = " ".join(str(row.get("profile_ru", "") or "").split())[:700]
    return out


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
        technology_ru=str(row.get("technology_ru", "")).strip()[:200],
        profile_ru=str(row.get("profile_ru", "")).strip()[:700],
    )


class RubricJudge:
    """Сценарий рубричной оценки кандидатов пачками."""

    def __init__(self, chain, prompts: PromptBuilder, batch_size: int = RUBRIC_BATCH,  # noqa: ANN001
                 maturity: tuple[MaturityCheck, MaturityRule] | None = None) -> None:
        self._chain = chain
        self._prompts = prompts
        self._batch = max(1, min(batch_size, 12))
        # Этап 5.4: второй проход зрелости для принятых кандидатов с научными источниками (None — выключен)
        self._maturity = maturity

    async def execute(self, query_text: str, items: Sequence[RubricItem], final_check: bool = False) -> RubricOutcome:
        """Оценивает до MAX_ITEMS кандидатов пачками; неразборчиво отвеченная пачка повторяется по одному.
        `final_check` — финальная проверка готовых карточек (строгие правила темы в промпте)."""
        items = list(items)[:MAX_ITEMS]
        if not items or self._chain.is_empty:
            return RubricOutcome((), used_fallback=True)
        verdicts: dict[str, RubricVerdict] = {}
        meta = {"provider": "", "model": ""}
        for start in range(0, len(items), self._batch):
            chunk = items[start : start + self._batch]
            got = await self._judge_chunk(query_text, chunk, meta, final_check)
            verdicts.update(got)
            missing = [item for item in chunk if item.candidate_id not in got]
            if missing and len(chunk) > 1:
                log.info("rubric.retry_single", start=start, missing=len(missing))
                for item in missing:
                    verdicts.update(await self._judge_chunk(query_text, [item], meta, final_check))
        ordered = tuple(verdicts[item.candidate_id] for item in items if item.candidate_id in verdicts)
        if self._maturity is not None and not final_check:
            ordered = await self._maturity_pass(items, ordered)
        log.info("rubric.done", requested=len(items), judged=len(ordered), provider=meta["provider"],
                 accepted=sum(1 for verdict in ordered if verdict.code == "R"), final_check=final_check)
        return RubricOutcome(ordered, meta["provider"], meta["model"], used_fallback=not ordered)

    async def _maturity_pass(self, items: Sequence[RubricItem],
                             verdicts: tuple[RubricVerdict, ...]) -> tuple[RubricVerdict, ...]:
        """Этап 5.4: принятый (R) кандидат только с научными источниками → N-MAT, если задача давняя и широкая,
        а новизна — улучшение известного или обзор. Сбой проверки оставляет вердикт без изменений."""
        check, rule = self._maturity  # type: ignore[misc]
        by_id = {item.candidate_id: item for item in items}
        targets = [v for v in verdicts if v.code == "R"
                   and research_only(s.source_type for s in by_id[v.candidate_id].sources)]
        if not targets:
            return verdicts
        inputs = []
        for verdict in targets:
            item = by_id[verdict.candidate_id]
            snippet = next((s.snippet for s in item.sources if s.snippet), "")
            description = " ".join(part for part in (verdict.profile_ru or item.composition_ru, snippet[:300]) if part)
            inputs.append(MaturityInput(verdict.candidate_id, verdict.technology_ru or item.title, description,
                                        tuple(s.title for s in item.sources[:3])))
        assessed = await check.assess(inputs)
        result, mature = [], 0
        for verdict in verdicts:
            assessment = assessed.get(verdict.candidate_id)
            if assessment is not None and rule.mature(assessment):
                mature += 1
                reason = (f"Проверка зрелости: задача «{assessment.task_en}» решается с {assessment.task_since_year} г.; "
                          f"в работе — {NOVELTY_RU.get(assessment.novelty_type, assessment.novelty_type)}.")
                result.append(replace(verdict, code="N-MAT", early_stage=False, reason_ru=reason[:300]))
            else:
                result.append(verdict)
        log.info("rubric.maturity_check", checked=len(targets), assessed=len(assessed), mature=mature,
                 misaligned=check.stats["misaligned"], failed_batches=check.stats["failed_batches"])
        return tuple(result)

    async def _judge_chunk(
        self, query_text: str, chunk: list[RubricItem], meta: dict, final_check: bool = False
    ) -> dict[str, RubricVerdict]:
        """Один вызов модели на пачку; каждый вердикт проверяется отдельно."""
        short_ids = {f"c{number}": item.candidate_id for number, item in enumerate(chunk, 1)}
        bundle = self._prompts.build_rubric_prompt(
            query_text, [payload_item(short, item) for short, item in zip(short_ids, chunk, strict=True)], final_check
        )
        try:
            outcome = await self._chain.complete(
                bundle.messages, json_schema=bundle.json_schema, max_tokens=RUBRIC_MAX_TOKENS,
                purpose=Purpose.JUDGE, prompt_version=bundle.prompt_version, request_sha256=bundle.request_sha256,
            )
            payload = parse(outcome.result.text)
        except (ProviderError, OutputRejected) as error:
            log.warning("rubric.batch_failed", size=len(chunk), reason=type(error).__name__, detail=str(error)[:200])
            return {}
        meta["provider"], meta["model"] = outcome.provider, str(getattr(outcome.result, "model", ""))
        item_schema = bundle.json_schema["properties"]["verdicts"]["items"]
        rows = payload.get("verdicts") if isinstance(payload.get("verdicts"), list) else []
        found: dict[str, RubricVerdict] = {}
        for row in rows:
            clean = coerce_verdict(row)
            if clean is None or validate(clean, item_schema):
                log.warning("rubric.verdict_rejected", detail=str(row)[:200])
                continue
            candidate_id = short_ids.get(clean["candidate_id"])
            if candidate_id is not None and candidate_id not in found:
                found[candidate_id] = to_verdict(candidate_id, clean)
        return found
