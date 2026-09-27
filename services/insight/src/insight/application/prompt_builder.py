"""Сборка промптов из шаблонов Jinja2 и их регистрация по sha256 (§7, §22 HANDOFF).

Тексты источников попадают в промпт как данные внутри JSON и сопровождаются явным указанием,
что содержимое поля `text` — данные, а не инструкции.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from insight.application.dto import LLMMessage, PromptDefinition
from insight.domain.entities import CandidateContext, EvidenceDocument
from insight.domain.values import Purpose

INSIGHT_PROMPT_VERSION = "insight_v1"
EXPAND_PROMPT_VERSION = "expand_v2"
INSIGHT_SCHEMA_FILE = "insight_llm_output.schema.json"
EXPAND_SCHEMA_FILE = "expand_llm_output.schema.json"
JUDGE_PROMPT_VERSION = "judge_v1"
JUDGE_SCHEMA_FILE = "judge_llm_output.schema.json"
RUBRIC_PROMPT_VERSION = "judge_v4"
RUBRIC_SCHEMA_FILE = "judge_v4_llm_output.schema.json"
FINALIZE_PROMPT_VERSION = "finalize_v3"
FINALIZE_SCHEMA_FILE = "finalize_v2_llm_output.schema.json"


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """Готовый промпт: сообщения, схема ответа и хеш запроса для журнала."""

    messages: tuple[LLMMessage, ...]
    json_schema: dict
    request_sha256: str
    prompt_version: str


class PromptBuilder:
    """Загружает шаблоны и схемы, собирает промпты и считает их контрольные суммы."""

    def __init__(self, prompts_dir: Path, schemas_dir: Path) -> None:
        self._prompts_dir = Path(prompts_dir)
        self._schemas_dir = Path(schemas_dir)
        self._environment = Environment(
            loader=FileSystemLoader(str(self._prompts_dir)),
            undefined=StrictUndefined,
            autoescape=False,  # noqa: S701 - шаблоны формируют текст промпта, не HTML
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._schemas = {
            INSIGHT_SCHEMA_FILE: self._read_schema(INSIGHT_SCHEMA_FILE),
            EXPAND_SCHEMA_FILE: self._read_schema(EXPAND_SCHEMA_FILE),
            JUDGE_SCHEMA_FILE: self._read_schema(JUDGE_SCHEMA_FILE),
            RUBRIC_SCHEMA_FILE: self._read_schema(RUBRIC_SCHEMA_FILE),
            FINALIZE_SCHEMA_FILE: self._read_schema(FINALIZE_SCHEMA_FILE),
        }

    def _read_schema(self, name: str) -> dict:
        """Читает нормативную схему ответа LLM."""
        return json.loads((self._schemas_dir / name).read_text(encoding="utf-8"))

    def schema(self, name: str) -> dict:
        """Схема ответа по имени файла."""
        return self._schemas[name]

    def schema_sha256(self, name: str) -> str:
        """Контрольная сумма схемы ответа (входит в ключ кеша инсайтов)."""
        return hashlib.sha256((self._schemas_dir / name).read_bytes()).hexdigest()

    def template_sha256(self, template: str) -> str:
        """Контрольная сумма шаблона (попадает в `prompt_versions`)."""
        return hashlib.sha256((self._prompts_dir / template).read_bytes()).hexdigest()

    def definitions(self) -> list[PromptDefinition]:
        """Версии промптов для регистрации при старте сервиса."""
        return [
            PromptDefinition(
                prompt_version=INSIGHT_PROMPT_VERSION,
                purpose=Purpose.INSIGHT,
                template_sha256=self.template_sha256("insight_v1.j2"),
                output_schema_version=f"{INSIGHT_SCHEMA_FILE}@1",
                description="Нарратив кандидата по доказательствам, только русский язык",
            ),
            PromptDefinition(
                prompt_version=EXPAND_PROMPT_VERSION,
                purpose=Purpose.EXPAND,
                template_sha256=self.template_sha256("expand_v2.j2"),
                output_schema_version=f"{EXPAND_SCHEMA_FILE}@1",
                description="Расширение запроса в поисковые фразы ru/en",
            ),
            PromptDefinition(
                prompt_version=JUDGE_PROMPT_VERSION,
                purpose=Purpose.JUDGE,
                template_sha256=self.template_sha256("judge_v1.j2"),
                output_schema_version=f"{JUDGE_SCHEMA_FILE}@1",
                description="Смысловая оценка кандидатов: технология, тема, стадия",
            ),
            PromptDefinition(
                prompt_version=RUBRIC_PROMPT_VERSION,
                purpose=Purpose.JUDGE,
                template_sha256=self.template_sha256("judge_v4.j2"),
                output_schema_version=f"{RUBRIC_SCHEMA_FILE}@1",
                description="Рубрика v4: код, критерии, стадия, тренд; извлечение технологии и профиля для классификатора",
            ),
            PromptDefinition(
                prompt_version=FINALIZE_PROMPT_VERSION,
                purpose=Purpose.INSIGHT,
                template_sha256=self.template_sha256("finalize_v3.j2"),
                output_schema_version=f"{FINALIZE_SCHEMA_FILE}@1",
                description="Пакетная доводка карточек v2: все поля на русском, резюме каждого источника, компании"
            ),
        ]

    def build_insight_prompt(
        self,
        candidate: CandidateContext,
        evidence: Sequence[EvidenceDocument],
        feedback: str = "",
    ) -> PromptBundle:
        """Промпт генерации нарратива; `feedback` передаёт замечания предыдущей попытки."""
        schema = self._schemas[INSIGHT_SCHEMA_FILE]
        payload = {
            "query": candidate.query_text,
            "candidate": {
                "title": candidate.title,
                "keyphrases": list(candidate.keyphrases),
                "score": round(candidate.score, 4),
                "decision": candidate.decision.value,
                "top_features": [
                    {
                        "label_ru": feature.label_ru,
                        "value": round(feature.value, 4),
                        "direction": feature.direction.value,
                    }
                    for feature in candidate.top_features
                ],
            },
            "evidence": [
                {
                    "n": position,
                    "document_id": document.document_id,
                    "title": document.title,
                    "url": document.url,
                    "date": document.published_at.date().isoformat() if document.published_at else "",
                    "type": document.source_type,
                    "trust": document.trust_level.value,
                    "language": document.language_code,
                    "text": document.text,
                }
                for position, document in enumerate(evidence, start=1)
            ],
        }
        system = self._environment.get_template("insight_v1.j2").render(
            schema=json.dumps(schema, ensure_ascii=False, indent=2), feedback=feedback
        )
        user = json.dumps(payload, ensure_ascii=False)
        return self._bundle(system, user, schema, INSIGHT_PROMPT_VERSION)

    def build_expand_prompt(self, query_text: str, domain_tags: Sequence[str]) -> PromptBundle:
        """Промпт расширения запроса в поисковые термины."""
        schema = self._schemas[EXPAND_SCHEMA_FILE]
        system = self._environment.get_template("expand_v2.j2").render(
            schema=json.dumps(schema, ensure_ascii=False, indent=2),
            domain_tags=list(domain_tags),
        )
        user = json.dumps({"query": query_text}, ensure_ascii=False)
        return self._bundle(system, user, schema, EXPAND_PROMPT_VERSION)

    def build_judge_prompt(self, query_text: str, candidates: Sequence[dict]) -> PromptBundle:
        """Промпт смысловой оценки кандидатов; `candidates` — словари с id, title, keyphrases, evidence."""
        schema = self._schemas[JUDGE_SCHEMA_FILE]
        system = self._environment.get_template("judge_v1.j2").render(
            schema=json.dumps(schema, ensure_ascii=False, indent=2)
        )
        user = json.dumps({"query": query_text, "candidates": list(candidates)}, ensure_ascii=False)
        return self._bundle(system, user, schema, JUDGE_PROMPT_VERSION)

    def build_rubric_prompt(self, query_text: str, candidates: Sequence[dict]) -> PromptBundle:
        """Промпт рубричной оценки: кандидаты с полным списком источников и сводкой их состава."""
        schema = self._schemas[RUBRIC_SCHEMA_FILE]
        system = self._environment.get_template("judge_v4.j2").render(
            schema=json.dumps(schema, ensure_ascii=False, indent=2)
        )
        user = json.dumps({"query": query_text, "candidates": list(candidates)}, ensure_ascii=False)
        return self._bundle(system, user, schema, RUBRIC_PROMPT_VERSION)

    def build_finalize_prompt(self, query_text: str, cards: Sequence[dict]) -> PromptBundle:
        """Промпт пакетной доводки показанных карточек."""
        schema = self._schemas[FINALIZE_SCHEMA_FILE]
        system = self._environment.get_template("finalize_v3.j2").render(
            schema=json.dumps(schema, ensure_ascii=False, indent=2)
        )
        user = json.dumps({"query": query_text, "cards": list(cards)}, ensure_ascii=False)
        return self._bundle(system, user, schema, FINALIZE_PROMPT_VERSION)

    @staticmethod
    def _bundle(system: str, user: str, schema: dict, version: str) -> PromptBundle:
        """Собирает сообщения и считает хеш запроса для журнала вызовов."""
        messages = (LLMMessage("system", system), LLMMessage("user", user))
        digest = hashlib.sha256(f"{system}\n{user}".encode()).hexdigest()
        return PromptBundle(
            messages=messages, json_schema=schema, request_sha256=digest, prompt_version=version
        )
