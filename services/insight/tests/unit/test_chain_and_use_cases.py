"""Цепочка провайдеров и сценарии: выбор провайдера, повторы, кеш, резерв, бюджет."""

from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from pathlib import Path

from insight.adapters.outbound.llm.fake_provider import FakeProvider, failure, rate_limited, timeout
from insight.application.dto import GenerateInsightCommand, LLMMessage
from insight.application.prompt_builder import PromptBuilder
from insight.application.provider_chain import ChainConfig, ProviderChain
from insight.application.use_cases.expand_query import ExpandQuery, Glossary
from insight.application.use_cases.generate_insight import (
    GenerateConfig,
    GenerateInsight,
    input_hash,
)
from insight.application.use_cases.get_provider_status import GetProviderStatus, RegisterPrompts
from insight.domain.errors import ProviderError, ProvidersUnavailable
from insight.domain.values import CallStatus, InsightStatus, ProviderName, Purpose, SummaryKind

from ..fakes import (
    FakeClock,
    InMemoryCallLog,
    InMemoryExpansionRepository,
    InMemoryInsightRepository,
    InMemoryPromptRegistry,
    make_candidate,
    make_evidence,
    new_id,
)

ROOT = Path(__file__).resolve().parents[4]
PROMPTS = ROOT / "services" / "insight" / "prompts"
SCHEMAS = ROOT / "services" / "insight" / "schemas"
GLOSSARY = ROOT / "services" / "insight" / "config" / "glossary_ru_en.yaml"
MESSAGES = (LLMMessage("system", "инструкция"), LLMMessage("user", '{"query": "тест"}'))


def build_chain(providers, clock=None, call_log=None, **config):  # noqa: ANN001, ANN003, ANN201
    """Цепочка провайдеров с управляемыми часами и журналом."""
    return ProviderChain(
        providers,
        call_log or InMemoryCallLog(),
        clock or FakeClock(),
        ChainConfig(**{"circuit_breaker_cooldown_seconds": 60.0, **config}),
    )


async def complete(chain):  # noqa: ANN001, ANN201
    """Короткий вызов цепочки для тестов."""
    return await chain.complete(
        MESSAGES,
        json_schema=None,
        max_tokens=256,
        purpose=Purpose.EXPAND,
        prompt_version="expand_v1",
        request_sha256="a" * 64,
    )


class ProviderChainTest(unittest.IsolatedAsyncioTestCase):
    """Порядок провайдеров, повторы, circuit breaker, бюджет и журнал."""

    async def test_uses_first_healthy_provider(self) -> None:
        primary = FakeProvider(name_value="gigachat")
        secondary = FakeProvider(name_value="yandexgpt")
        outcome = await complete(build_chain([primary, secondary]))
        self.assertEqual(outcome.provider, "gigachat")
        self.assertEqual(len(secondary.calls), 0)

    async def test_switches_to_next_provider_on_timeout(self) -> None:
        primary = FakeProvider(name_value="gigachat", errors=[timeout()])
        secondary = FakeProvider(name_value="yandexgpt")
        outcome = await complete(build_chain([primary, secondary]))
        self.assertEqual(outcome.provider, "yandexgpt")

    async def test_retries_same_provider_on_rate_limit(self) -> None:
        clock = FakeClock()
        primary = FakeProvider(name_value="gigachat", errors=[rate_limited(1.0)])
        outcome = await complete(build_chain([primary], clock=clock))
        self.assertEqual(outcome.provider, "gigachat")
        # Пауза не короче RATE_LIMIT_BACKOFF_SECONDS: GigaChat не присылает Retry-After,
        # а пяти секунд по умолчанию его окну ограничения не хватало (лог 23.09: 6 ответов 429).
        self.assertEqual(clock.slept, [10.0])
        self.assertEqual(len(primary.calls), 2)

    async def test_gives_up_after_rate_limit_retries(self) -> None:
        primary = FakeProvider(
            name_value="gigachat", errors=[rate_limited(0.1), rate_limited(0.1), rate_limited(0.1)]
        )
        with self.assertRaises(ProviderError):
            await complete(build_chain([primary]))

    async def test_circuit_breaker_opens_and_recovers(self) -> None:
        clock = FakeClock()
        primary = FakeProvider(name_value="gigachat", errors=[failure(), failure()])
        secondary = FakeProvider(name_value="yandexgpt")
        chain = build_chain([primary, secondary], clock=clock, circuit_breaker_failures=2)
        await complete(chain)
        await complete(chain)
        self.assertFalse(chain.states()[0].healthy)
        self.assertEqual(chain.active_provider, "yandexgpt")
        clock.advance(61)
        self.assertTrue(chain.states()[0].healthy)
        self.assertEqual(chain.active_provider, "gigachat")

    async def test_semaphore_reflects_max_concurrency(self) -> None:
        provider = FakeProvider(name_value="gigachat", concurrency=1)
        chain = build_chain([provider])
        self.assertEqual(chain.states()[0].max_concurrency, 1)

    async def test_call_log_has_no_prompt_text(self) -> None:
        call_log = InMemoryCallLog()
        await complete(build_chain([FakeProvider()], call_log=call_log))
        self.assertEqual(len(call_log.records), 1)
        record = call_log.records[0]
        self.assertIs(record.status, CallStatus.OK)
        serialized = json.dumps(asdict(record), default=str, ensure_ascii=False)
        self.assertNotIn("инструкция", serialized)
        self.assertNotIn("query", serialized)

    async def test_failed_call_is_logged_with_status(self) -> None:
        call_log = InMemoryCallLog()
        chain = build_chain([FakeProvider(errors=[timeout()])], call_log=call_log)
        with self.assertRaises(ProviderError):
            await complete(chain)
        self.assertIs(call_log.records[0].status, CallStatus.TIMEOUT)

    async def test_daily_budget_blocks_calls(self) -> None:
        call_log = InMemoryCallLog(tokens_today=10_000)
        chain = build_chain([FakeProvider()], call_log=call_log, daily_token_budget=5_000)
        with self.assertRaises(ProviderError):
            await complete(chain)

    async def test_empty_chain_reports_none(self) -> None:
        chain = build_chain([])
        self.assertTrue(chain.is_empty)
        self.assertEqual(chain.active_provider, "none")


class GenerateInsightTest(unittest.IsolatedAsyncioTestCase):
    """Сценарий генерации нарратива на детерминированном провайдере."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.prompts = PromptBuilder(PROMPTS, SCHEMAS)
        self.insights = InMemoryInsightRepository()
        self.provider = FakeProvider()
        self.chain = build_chain([self.provider], clock=self.clock)
        self.use_case = GenerateInsight(
            self.chain, self.prompts, self.insights, self.clock, GenerateConfig()
        )
        self.command = GenerateInsightCommand(
            idempotency_key="job-1:cand-1:insight:insight_v1",
            candidate=make_candidate(),
            evidence=(make_evidence(1), make_evidence(2, language="ru")),
        )

    async def test_generates_grounded_narrative(self) -> None:
        insight = await self.use_case.execute(self.command)
        self.assertIs(insight.status, InsightStatus.GENERATED)
        self.assertTrue(insight.grounding.passed, insight.grounding.summary)
        self.assertEqual(insight.provenance.provider, "fake")
        self.assertTrue(insight.insight_id)

    async def test_summaries_cover_every_document(self) -> None:
        insight = await self.use_case.execute(self.command)
        self.assertEqual(len(insight.summaries), 2)
        kinds = {summary.document_id: summary.kind for summary in insight.summaries}
        self.assertIs(kinds[self.command.evidence[0].document_id], SummaryKind.GENERATIVE_SUMMARY)
        self.assertIs(kinds[self.command.evidence[1].document_id], SummaryKind.ORIGINAL_RU)

    async def test_doc_refs_stripped_from_title(self) -> None:
        insight = await self.use_case.execute(self.command)
        self.assertNotIn("[doc:", insight.narrative.title_ru)

    async def test_repeat_by_idempotency_key_uses_cache(self) -> None:
        first = await self.use_case.execute(self.command)
        calls = len(self.provider.calls)
        second = await self.use_case.execute(self.command)
        self.assertTrue(second.from_cache)
        self.assertEqual(second.insight_id, first.insight_id)
        self.assertEqual(len(self.provider.calls), calls)

    async def test_cache_by_input_hash(self) -> None:
        await self.use_case.execute(self.command)
        other_key = GenerateInsightCommand(
            idempotency_key="job-2:cand-1:insight:insight_v1",
            candidate=self.command.candidate,
            evidence=self.command.evidence,
        )
        calls = len(self.provider.calls)
        result = await self.use_case.execute(other_key)
        self.assertTrue(result.from_cache)
        self.assertEqual(len(self.provider.calls), calls)

    async def test_invalid_json_retried_then_fallback(self) -> None:
        provider = FakeProvider(responses=["не json", "тоже не json"])
        use_case = GenerateInsight(
            build_chain([provider], clock=self.clock),
            self.prompts,
            self.insights,
            self.clock,
            GenerateConfig(max_attempts=2),
        )
        insight = await use_case.execute(self.command)
        self.assertIs(insight.status, InsightStatus.FALLBACK_EXTRACTIVE)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(insight.provenance.provider, ProviderName.NONE.value)

    async def test_ungrounded_answer_is_retried_with_feedback(self) -> None:
        bad = json.dumps(
            {
                "title_ru": "Технология",
                "description_ru": "Рынок вырастет на 300 процентов уже в ближайшее время, "
                "и это подтверждают независимые обзоры отрасли.",
                "advantage_ru": "Преимущество в кратном снижении затрат на инфраструктуру.",
                "case_example": {
                    "text_ru": "Компания внедрила решение на 500 площадках за один квартал.",
                    "document_id": new_id(999),
                },
                "explanation_ru": "Модель считает наблюдение ранним, потому что так решила модель "
                "на основании внутренних соображений и общих представлений.",
                "source_summaries": [
                    {"document_id": self.command.evidence[0].document_id,
                     "summary_ru": "Источник описывает исследование прототипа."},
                ],
            },
            ensure_ascii=False,
        )
        provider = FakeProvider(responses=[bad])
        use_case = GenerateInsight(
            build_chain([provider], clock=self.clock),
            self.prompts,
            self.insights,
            self.clock,
            GenerateConfig(max_attempts=2),
        )
        insight = await use_case.execute(self.command)
        self.assertIs(insight.status, InsightStatus.GENERATED)
        self.assertEqual(len(provider.calls), 2)
        second_prompt = provider.calls[1][0].content
        self.assertIn("ПРЕДЫДУЩАЯ ПОПЫТКА ОТКЛОНЕНА", second_prompt)

    async def test_fallback_forbidden_raises(self) -> None:
        provider = FakeProvider(errors=[failure(), failure()])
        use_case = GenerateInsight(
            build_chain([provider], clock=self.clock),
            self.prompts,
            self.insights,
            self.clock,
            GenerateConfig(max_attempts=2),
        )
        command = GenerateInsightCommand(
            idempotency_key="job-3:cand-1:insight:insight_v1",
            candidate=self.command.candidate,
            evidence=self.command.evidence,
            allow_fallback=False,
        )
        with self.assertRaises(ProvidersUnavailable):
            await use_case.execute(command)

    async def test_no_providers_gives_fallback(self) -> None:
        use_case = GenerateInsight(
            build_chain([]), self.prompts, self.insights, self.clock, GenerateConfig()
        )
        insight = await use_case.execute(self.command)
        self.assertIs(insight.status, InsightStatus.FALLBACK_EXTRACTIVE)
        self.assertTrue(insight.grounding.passed)

    def test_input_hash_is_order_independent(self) -> None:
        first = input_hash(self.command.candidate, self.command.evidence, "insight_v1")
        reversed_evidence = tuple(reversed(self.command.evidence))
        second = input_hash(self.command.candidate, reversed_evidence, "insight_v1")
        self.assertEqual(first, second)

    def test_input_hash_changes_with_evidence(self) -> None:
        first = input_hash(self.command.candidate, self.command.evidence, "insight_v1")
        second = input_hash(self.command.candidate, self.command.evidence[:1], "insight_v1")
        self.assertNotEqual(first, second)


class ExpandQueryTest(unittest.IsolatedAsyncioTestCase):
    """Расширение запроса: LLM, кеш и резерв по глоссарию."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.prompts = PromptBuilder(PROMPTS, SCHEMAS)
        self.expansions = InMemoryExpansionRepository()
        self.glossary = Glossary.load(GLOSSARY)

    def build(self, providers):  # noqa: ANN001, ANN201
        """Сценарий расширения на заданных провайдерах."""
        return ExpandQuery(
            build_chain(providers, clock=self.clock), self.prompts, self.expansions, self.glossary
        )

    async def test_uses_llm_terms(self) -> None:
        provider = FakeProvider(
            responses=[
                json.dumps(
                    {
                        "ru_terms": ["нейроморфные чипы", "спайковые процессоры"],
                        "en_terms": ["neuromorphic chips", "spiking processors"],
                        "domain_tags": ["edge"],
                    },
                    ensure_ascii=False,
                )
            ]
        )
        expansion = await self.build([provider]).execute("нейроморфные чипы")
        self.assertFalse(expansion.used_fallback)
        self.assertIn("спайковые процессоры", expansion.ru_terms)
        self.assertEqual(expansion.domain_tags, ("edge",))

    async def test_cache_prevents_second_call(self) -> None:
        provider = FakeProvider()
        use_case = self.build([provider])
        await use_case.execute("слабые сигналы в ИИ")
        calls = len(provider.calls)
        await use_case.execute("  Слабые   сигналы В ИИ ")
        self.assertEqual(len(provider.calls), calls)

    async def test_fallback_on_provider_error(self) -> None:
        provider = FakeProvider(errors=[failure(), failure(), failure()])
        expansion = await self.build([provider]).execute("слабые сигналы в кибербезопасности")
        self.assertTrue(expansion.used_fallback)
        self.assertEqual(expansion.provenance.provider, ProviderName.NONE.value)
        self.assertIn("ai_security", expansion.domain_tags)

    async def test_fallback_translates_by_glossary(self) -> None:
        expansion = await self.build([]).execute("кибербезопасность")
        self.assertTrue(expansion.used_fallback)
        self.assertEqual(expansion.en_terms, ("cybersecurity",))

    async def test_invalid_schema_falls_back(self) -> None:
        provider = FakeProvider(responses=[json.dumps({"ru_terms": []}, ensure_ascii=False)])
        expansion = await self.build([provider]).execute("финтех")
        self.assertTrue(expansion.used_fallback)


class ProviderStatusTest(unittest.IsolatedAsyncioTestCase):
    """Статус провайдеров и регистрация промптов."""

    async def test_status_lists_providers(self) -> None:
        chain = build_chain([FakeProvider(name_value="gigachat", concurrency=1)])
        states, active = GetProviderStatus(chain).execute()
        self.assertEqual(active, "gigachat")
        self.assertEqual(states[0].max_concurrency, 1)
        self.assertTrue(states[0].healthy)

    async def test_register_prompts(self) -> None:
        registry = InMemoryPromptRegistry()
        count = await RegisterPrompts(registry, PromptBuilder(PROMPTS, SCHEMAS)).execute()
        self.assertEqual(count, 3)
        purposes = {definition.purpose for definition in registry.definitions}
        self.assertEqual({item.value for item in purposes}, {"insight", "expand", "judge"})


if __name__ == "__main__":
    unittest.main()
