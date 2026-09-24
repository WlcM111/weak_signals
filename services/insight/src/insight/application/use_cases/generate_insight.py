"""Сценарий GenerateInsight: кандидат + доказательства → русскоязычный нарратив (§7 HANDOFF).

Порядок: кеш по ключу идемпотентности → кеш по содержимому входа → генерация через цепочку
провайдеров с проверкой схемы и обоснованности → при исчерпании попыток экстрактивный резерв
(или отказ, если клиент его запретил).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from insight.application import fallback as fallback_module
from insight.application.dto import GenerateInsightCommand, GenerationAttempt
from insight.application.json_output import OutputRejected, parse_and_validate
from insight.application.ports import InsightRepository, MetricsSink, NullMetrics
from insight.application.prompt_builder import INSIGHT_PROMPT_VERSION, INSIGHT_SCHEMA_FILE, PromptBuilder
from insight.application.provider_chain import ProviderChain
from insight.domain import grounding as grounding_module
from insight.domain.entities import (
    CandidateContext,
    EvidenceDocument,
    GroundingCheck,
    Insight,
    Narrative,
    SourceSummary,
)
from insight.domain.errors import ProviderError, ProvidersUnavailable
from insight.domain.values import (
    MAX_SUMMARY,
    InsightStatus,
    ProviderName,
    Provenance,
    Purpose,
    SummaryKind,
)
from ws_common.clock import Clock
from ws_common.logging import get_logger

DEFAULT_MAX_TOKENS = 1200
log = get_logger("insight.generate_insight")


@dataclass(frozen=True, slots=True)
class GenerateConfig:
    """Параметры генерации нарратива."""

    max_attempts: int = 2
    cache_days: int = 7
    min_features: int = 2
    min_cyrillic_share: float = 0.6
    default_max_tokens: int = DEFAULT_MAX_TOKENS


def input_hash(candidate: CandidateContext, evidence: Sequence[EvidenceDocument], prompt_version: str) -> str:
    """Канонический хеш всего, что определяет ответ модели.

    В ключ входят запрос, ключевые фразы, признаки, содержимое документов и версия шаблона со схемой:
    раньше ключ строился только из идентификаторов документов и константы «insight_v1», и после
    правки промпта или текста источника кеш мог вернуть карточку, написанную по старому входу.
    """
    payload = {
        "candidate_id": candidate.candidate_id,
        "title": candidate.title,
        "score": round(candidate.score, 4),
        "decision": candidate.decision.value,
        "query_text": candidate.query_text,
        "keyphrases": list(candidate.keyphrases),
        "features": [
            [feature.feature_name, round(feature.value, 4), feature.direction.value]
            for feature in candidate.top_features
        ],
        "evidence": sorted(
            [
                document.document_id,
                hashlib.sha256(f"{document.title}\n{document.text}".encode("utf-8")).hexdigest()[:16],
            ]
            for document in evidence
        ),
        "prompt_version": prompt_version,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GenerateInsight:
    """Строит нарратив кандидата, проверяя каждый ответ модели доказательствами."""

    def __init__(
        self,
        chain: ProviderChain,
        prompts: PromptBuilder,
        insights: InsightRepository,
        clock: Clock,
        config: GenerateConfig,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._chain = chain
        self._prompts = prompts
        # Версия промпта для ключа кеша: правка шаблона или схемы делает прежние карточки неактуальными.
        self._prompt_identity = (
            f"{INSIGHT_PROMPT_VERSION}@{prompts.template_sha256('insight_v1.j2')[:16]}"
            f"@{prompts.schema_sha256(INSIGHT_SCHEMA_FILE)[:16]}"
        )
        self._insights = insights
        self._clock = clock
        self._config = config
        self._metrics: MetricsSink = metrics or NullMetrics()

    async def execute(self, command: GenerateInsightCommand) -> Insight:
        """Возвращает готовый инсайт; повтор с тем же ключом не обращается к модели."""
        cached = await self._insights.get_by_idempotency_key(command.idempotency_key)
        if cached is not None:
            return self._as_cached(cached)
        digest = input_hash(command.candidate, command.evidence, self._prompt_identity)
        not_older_than = self._clock.now() - timedelta(days=self._config.cache_days)
        by_content = await self._insights.get_by_input_hash(digest, not_older_than)
        if by_content is not None:
            return self._as_cached(by_content)
        insight = await self._generate(command, digest)
        saved = await self._insights.save(insight)
        self._metrics.insight_status(saved.status.value)
        log.info(
            "insight.generated",
            status=saved.status.value,
            grounding=saved.grounding.passed,
            from_cache=False,
            attempts=saved.attempts,
            provider=saved.provenance.provider,
        )
        return saved

    async def _generate(self, command: GenerateInsightCommand, digest: str) -> Insight:
        """Цикл попыток генерации с обратной связью по нарушениям."""
        attempt = GenerationAttempt()
        last_reason = ""
        max_tokens = command.max_output_tokens or self._config.default_max_tokens
        while attempt.number < self._config.max_attempts and not self._chain.is_empty:
            attempt.number += 1
            bundle = self._prompts.build_insight_prompt(
                command.candidate, command.evidence, feedback=attempt.feedback
            )
            try:
                outcome = await self._chain.complete(
                    bundle.messages,
                    json_schema=bundle.json_schema,
                    max_tokens=max_tokens,
                    purpose=Purpose.INSIGHT,
                    prompt_version=bundle.prompt_version,
                    request_sha256=bundle.request_sha256,
                    idempotency_key=command.idempotency_key,
                )
                payload = parse_and_validate(outcome.result.text, bundle.json_schema)
            except OutputRejected as error:
                last_reason = error.reason
                attempt.failures = [error.reason]
                continue
            except ProviderError as error:
                last_reason = f"{error.error_code}: {error.message}"
                break
            narrative, summaries = self._build(payload, command.evidence)
            check = grounding_module.check(
                narrative,
                command.evidence,
                command.candidate.top_features,
                min_features=self._config.min_features,
                min_cyrillic_share=self._config.min_cyrillic_share,
                score=command.candidate.score,
            )
            if check.passed:
                return self._accept(command, digest, narrative, summaries, check, outcome, attempt.number)
            last_reason = check.summary
            attempt.failures = list(check.hard_failures) + list(check.soft_failures)
            log.warning(
                "insight.rejected",
                attempt=attempt.number,
                hard=len(check.hard_failures),
                soft=len(check.soft_failures),
            )
        if not command.allow_fallback:
            raise ProvidersUnavailable(
                f"не удалось сформировать обоснованный нарратив: {last_reason or 'нет провайдеров'}"
            )
        return self._fallback(command, digest, last_reason, attempt.number)

    def _build(
        self, payload: dict, evidence: Sequence[EvidenceDocument]
    ) -> tuple[Narrative, tuple[SourceSummary, ...]]:
        """Собирает нарратив и резюме из проверенного по схеме ответа модели."""
        case = payload["case_example"]
        narrative = Narrative(
            title_ru=grounding_module.strip_doc_refs(payload["title_ru"]),
            description_ru=payload["description_ru"],
            advantage_ru=payload["advantage_ru"],
            case_example_ru=case["text_ru"],
            explanation_ru=payload["explanation_ru"],
            case_document_id=case["document_id"],
        )
        known = {document.document_id: document for document in evidence}
        order = {document.document_id: index for index, document in enumerate(evidence, start=1)}
        summaries: list[SourceSummary] = []
        for item in payload["source_summaries"]:
            document = known.get(item["document_id"])
            if document is None:
                continue
            summaries.append(
                SourceSummary(
                    document_id=document.document_id,
                    summary_ru=grounding_module.strip_doc_refs(item["summary_ru"])[:MAX_SUMMARY],
                    kind=SummaryKind.ORIGINAL_RU
                    if document.is_russian
                    else SummaryKind.GENERATIVE_SUMMARY,
                    position=order[document.document_id],
                )
            )
        summaries.extend(
            summary
            for summary in fallback_module.extractive_summaries(evidence)
            if summary.document_id not in {item.document_id for item in summaries}
        )
        ordered = sorted(summaries, key=lambda item: item.position)
        renumbered = tuple(
            SourceSummary(
                document_id=item.document_id,
                summary_ru=item.summary_ru,
                kind=item.kind,
                position=position,
            )
            for position, item in enumerate(ordered, start=1)
        )
        return narrative, renumbered

    def _accept(
        self,
        command: GenerateInsightCommand,
        digest: str,
        narrative: Narrative,
        summaries: tuple[SourceSummary, ...],
        check: GroundingCheck,
        outcome,  # noqa: ANN001 - ChainOutcome
        attempts: int,
    ) -> Insight:
        """Собирает инсайт из принятого ответа модели."""
        return Insight(
            idempotency_key=command.idempotency_key,
            input_hash=digest,
            candidate_id=command.candidate.candidate_id,
            prompt_version=INSIGHT_PROMPT_VERSION,
            status=InsightStatus.GENERATED,
            narrative=narrative,
            grounding=check,
            summaries=summaries,
            attempts=attempts,
            provenance=Provenance(
                provider=outcome.provider,
                model=outcome.result.model,
                prompt_version=INSIGHT_PROMPT_VERSION,
                prompt_tokens=outcome.result.prompt_tokens,
                completion_tokens=outcome.result.completion_tokens,
                attempts=attempts,
                latency_ms=outcome.result.latency_ms,
            ),
        )

    def _fallback(
        self, command: GenerateInsightCommand, digest: str, reason: str, attempts: int
    ) -> Insight:
        """Экстрактивный результат, когда модель недоступна или не прошла проверку."""
        log.warning("insight.fallback", reason=reason[:200], attempts=attempts)
        return Insight(
            idempotency_key=command.idempotency_key,
            input_hash=digest,
            candidate_id=command.candidate.candidate_id,
            prompt_version=INSIGHT_PROMPT_VERSION,
            status=InsightStatus.FALLBACK_EXTRACTIVE,
            narrative=fallback_module.extractive_narrative(command.candidate, command.evidence),
            grounding=fallback_module.fallback_grounding(reason),
            summaries=fallback_module.extractive_summaries(command.evidence),
            attempts=attempts,
            provenance=Provenance(
                provider=ProviderName.NONE.value, prompt_version=INSIGHT_PROMPT_VERSION, attempts=attempts
            ),
        )

    def _as_cached(self, insight: Insight) -> Insight:
        """Помечает результат как взятый из кеша."""
        log.info("insight.generated", status=insight.status.value, from_cache=True)
        self._metrics.insight_status(insight.status.value)
        return Insight(
            idempotency_key=insight.idempotency_key,
            input_hash=insight.input_hash,
            candidate_id=insight.candidate_id,
            prompt_version=insight.prompt_version,
            status=insight.status,
            narrative=insight.narrative,
            grounding=insight.grounding,
            provenance=insight.provenance,
            summaries=insight.summaries,
            insight_id=insight.insight_id,
            attempts=insight.attempts,
            from_cache=True,
            created_at=insight.created_at,
        )
