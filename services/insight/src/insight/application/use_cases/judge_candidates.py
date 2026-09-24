"""Смысловая оценка кандидатов перед нарративом: один пакетный вызов LLM на задание.

Прогоны 22–24.09 показали, что ни одна стадия не проверяет, конкретная ли это технология и относится ли она
к запросу: обзоры, общие понятия и статьи не по теме проходили и с источниками высокой доверенности. Модель
получает название, ключевые фразы и фрагменты доказательств каждого кандидата и возвращает вердикт и
релевантность. Кандидаты передаются под короткими номерами `c1…cN`: длинные идентификаторы модель может
исказить. Любой отказ модели или невалидный ответ даёт пустую оценку — вызывающая сторона сохраняет прежнее
поведение, а не теряет выдачу.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from insight.application.json_output import OutputRejected, parse_and_validate
from insight.application.prompt_builder import PromptBuilder
from insight.domain.errors import ProviderError
from insight.domain.values import Purpose
from ws_common.logging import get_logger

# До 40 вердиктов по ~100 токенов: с запасом, чтобы обрезанный JSON не превращал оценку в резерв.
JUDGE_MAX_TOKENS = 4000
MAX_ITEMS = 40
MAX_KEYPHRASES = 8
MAX_EVIDENCE = 3
MAX_EVIDENCE_CHARS = 300

log = get_logger("insight.judge")


@dataclass(frozen=True, slots=True)
class JudgeItem:
    """Кандидат для оценки."""

    candidate_id: str
    title: str
    keyphrases: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """Вердикт по кандидату."""

    candidate_id: str
    verdict: str
    relevance: int
    reason_ru: str


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """Результат оценки; `used_fallback` — оценка не выполнена."""

    verdicts: tuple[JudgeVerdict, ...]
    provider: str = ""
    model: str = ""
    used_fallback: bool = False


class JudgeCandidates:
    """Сценарий смысловой оценки кандидатов."""

    def __init__(self, chain, prompts: PromptBuilder) -> None:  # noqa: ANN001 - ProviderChain
        self._chain = chain
        self._prompts = prompts

    async def execute(self, query_text: str, items: Sequence[JudgeItem]) -> JudgeOutcome:
        """Оценивает до MAX_ITEMS кандидатов одним вызовом модели."""
        items = list(items)[:MAX_ITEMS]
        if not items or self._chain.is_empty:
            return JudgeOutcome((), used_fallback=True)
        short_ids = {f"c{number}": item.candidate_id for number, item in enumerate(items, 1)}
        payload_items = [
            {
                "id": short_id,
                "title": item.title,
                "keyphrases": list(item.keyphrases[:MAX_KEYPHRASES]),
                "evidence": [text[:MAX_EVIDENCE_CHARS] for text in item.evidence[:MAX_EVIDENCE]],
            }
            for short_id, item in zip(short_ids, items, strict=True)
        ]
        bundle = self._prompts.build_judge_prompt(query_text, payload_items)
        try:
            outcome = await self._chain.complete(
                bundle.messages,
                json_schema=bundle.json_schema,
                max_tokens=JUDGE_MAX_TOKENS,
                purpose=Purpose.JUDGE,
                prompt_version=bundle.prompt_version,
                request_sha256=bundle.request_sha256,
            )
            payload = parse_and_validate(outcome.result.text, bundle.json_schema)
        except (ProviderError, OutputRejected) as error:
            log.warning("judge.fallback", reason=type(error).__name__, detail=str(error)[:200])
            return JudgeOutcome((), used_fallback=True)
        verdicts: list[JudgeVerdict] = []
        seen: set[str] = set()
        for row in payload.get("verdicts", []):
            candidate_id = short_ids.get(str(row.get("candidate_id", "")))
            if candidate_id is None or candidate_id in seen:
                continue
            seen.add(candidate_id)
            verdicts.append(
                JudgeVerdict(candidate_id, row["verdict"], int(row["relevance"]), str(row["reason_ru"]).strip()[:300])
            )
        log.info("judge.done", requested=len(items), judged=len(verdicts), provider=outcome.provider)
        return JudgeOutcome(tuple(verdicts), outcome.provider, str(getattr(outcome.result, "model", "")), False)
