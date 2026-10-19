"""Второй проход зрелости после рубрики (этап 5.4).

Причина (замеры 18.10.2026): судья рубрики пропускает зрелые темы как «ранние» — 31–32 из 35 зрелых карточек при
версиях judge_v6–v8, когда критерий зрелости спрятан среди других критериев. Здесь модель отвечает на два узких
вопроса по каждому кандидату: возраст и распространённость задачи (по общеизвестным знаниям) и вид новизны (только по
тексту кандидата). Решение «зрелое» принимает не модель, а заданное правило по этим полям.

Охват — только кандидаты с научными, патентными и кодовыми источниками: карточки с сообщениями СМИ и пресс-релизами не
трогаются (у них зрелых 2 из 50 в прогоне 18.10, а слабых — 39). Сбой модели, неразборчивый ответ или ответ не про
того кандидата (сверка по title_echo) — кандидат остаётся как был: ошибка не превращается в исключение.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from insight.application.json_output import OutputRejected, parse, validate
from insight.application.prompt_builder import PromptBuilder
from insight.domain.errors import ProviderError
from insight.domain.values import Purpose
from ws_common.logging import get_logger

log = get_logger(__name__)

NOVELTY_TYPES = ("new_mechanism", "new_application", "improvement", "survey", "news")
NOVELTY_RU = {
    "new_mechanism": "новый механизм",
    "new_application": "новое применение",
    "improvement": "улучшение известного метода",
    "survey": "обзор или общее описание области",
    "news": "сообщение о компании или продукте",
}
MARKET_TYPES = frozenset({"NEWS", "INDUSTRY_MEDIA", "PRESS_RELEASE", "ANALYTICAL_REPORT", "CORPORATE_BLOG", "GOVERNMENT"})
BATCH = 5
MAX_TOKENS = 1500


@dataclass(frozen=True, slots=True)
class MaturityInput:
    """Кандидат для второго прохода: технология, её описание и заголовки источников."""

    key: str
    title: str
    description: str = ""
    sources: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaturityAssessment:
    key: str
    task_en: str
    task_since_year: int
    task_established: bool
    novelty_type: str


@dataclass(frozen=True, slots=True)
class MaturityRule:
    """Правило «зрелое»: давняя и широкая задача и новизна вида «улучшение» или «обзор»."""

    max_year: int = 2015
    novelty: frozenset[str] = field(default_factory=lambda: frozenset({"improvement", "survey"}))

    def mature(self, assessment: MaturityAssessment) -> bool:
        return (assessment.task_established and assessment.novelty_type in self.novelty
                and 1900 < assessment.task_since_year <= self.max_year)

    @staticmethod
    def parse_novelty(text: str) -> frozenset[str]:
        chosen = frozenset(part.strip() for part in text.split(",") if part.strip() in NOVELTY_TYPES)
        return chosen or frozenset({"improvement", "survey"})


def research_only(source_types: Iterable[str]) -> bool:
    """Только научные, патентные, кодовые и прочие неновостные источники; хотя бы один источник."""
    types = [t for t in source_types]
    return bool(types) and not (set(types) & MARKET_TYPES)


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", " ", text.casefold()).strip()


def echo_matches(echo: str, title: str) -> bool:
    """Ответ относится к этому кандидату: эхо совпадает с началом названия по словам (≥ 60 % слов эха)."""
    words, head = _norm(echo).split(), set(_norm(title)[:120].split())
    return bool(words) and sum(1 for word in words if word in head) >= 0.6 * len(words)


class MaturityCheck:
    """Оценка возраста задачи и вида новизны пачками по BATCH кандидатов."""

    def __init__(self, chain, prompts: PromptBuilder, batch_size: int = BATCH) -> None:  # noqa: ANN001
        self._chain = chain
        self._prompts = prompts
        self._batch = max(1, min(batch_size, 10))
        self.stats = {"requested": 0, "assessed": 0, "misaligned": 0, "failed_batches": 0}

    async def assess(self, items: Sequence[MaturityInput]) -> dict[str, MaturityAssessment]:
        found: dict[str, MaturityAssessment] = {}
        self.stats["requested"] += len(items)
        if not items or self._chain.is_empty:
            return found
        for start in range(0, len(items), self._batch):
            chunk = list(items[start : start + self._batch])
            short = {f"m{n}": item for n, item in enumerate(chunk, 1)}
            payload = [{"id": sid, "title": item.title[:200], "description": item.description[:600],
                        "sources": [s[:150] for s in item.sources[:3]]} for sid, item in short.items()]
            bundle = self._prompts.build_maturity_check_prompt(payload)
            try:
                outcome = await self._chain.complete(
                    bundle.messages, json_schema=bundle.json_schema, max_tokens=MAX_TOKENS, purpose=Purpose.JUDGE,
                    prompt_version=bundle.prompt_version, request_sha256=bundle.request_sha256)
                data = parse(outcome.result.text)
            except (ProviderError, OutputRejected) as error:
                self.stats["failed_batches"] += 1
                log.warning("maturity.batch_failed", size=len(chunk), reason=type(error).__name__)
                continue
            item_schema = bundle.json_schema["properties"]["items"]["items"]
            for row in data.get("items") if isinstance(data.get("items"), list) else []:
                if not isinstance(row, dict) or validate(row, item_schema):
                    continue
                item = short.get(str(row.get("id")))
                if item is None or item.key in found:
                    continue
                if not echo_matches(str(row.get("title_echo", "")), item.title):
                    self.stats["misaligned"] += 1
                    log.warning("maturity.misaligned", key=item.key)
                    continue
                found[item.key] = MaturityAssessment(item.key, str(row["task_en"]).strip()[:80],
                                                     int(row["task_since_year"]), bool(row["task_established"]),
                                                     str(row["novelty_type"]))
        self.stats["assessed"] += len(found)
        return found
