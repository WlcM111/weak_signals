"""Ограничение частоты — не поломка провайдера (docs/quality/ERROR_ANALYSIS.md, E15).

Лог insight 23.09: 56 успешных вызовов GigaChat, 6 ответов 429 и 15 карточек в резерве с причиной
«ни один провайдер LLM не настроен или все недоступны». Три ответа 429 одной карточки размыкали
предохранитель на минуту, и все следующие карточки уходили в резерв мгновенно, не обращаясь к модели.
"""

from __future__ import annotations

import unittest

from insight.adapters.outbound.llm.fake_provider import FakeProvider, rate_limited, timeout
from insight.application.provider_chain import RATE_LIMIT_BACKOFF_MAX_SECONDS, RATE_LIMIT_BACKOFF_SECONDS
from insight.domain.errors import ProviderError, ProviderRateLimited

from ..fakes import FakeClock
from .test_chain_and_use_cases import build_chain, complete


class RateLimitBackpressureTest(unittest.IsolatedAsyncioTestCase):
    """Сценарий из лога: после серии 429 следующая карточка ждёт окно и обращается к модели."""

    async def test_rate_limits_do_not_disable_provider_for_next_card(self) -> None:
        clock = FakeClock()
        provider = FakeProvider(name_value="gigachat", errors=[rate_limited(0.1)] * 3)
        chain = build_chain([provider], clock=clock)
        with self.assertRaises(ProviderRateLimited):
            await complete(chain)
        calls_before = len(provider.calls)
        outcome = await complete(chain)
        self.assertEqual(outcome.provider, "gigachat", "следующая карточка должна дойти до модели")
        self.assertEqual(len(provider.calls), calls_before + 1)

    async def test_next_card_waits_for_rate_limit_window(self) -> None:
        clock = FakeClock()
        provider = FakeProvider(name_value="gigachat", errors=[rate_limited(0.1)] * 3)
        chain = build_chain([provider], clock=clock)
        with self.assertRaises(ProviderRateLimited):
            await complete(chain)
        clock.slept.clear()
        await complete(chain)
        self.assertEqual(len(clock.slept), 1)
        self.assertGreaterEqual(clock.slept[0], RATE_LIMIT_BACKOFF_SECONDS)

    async def test_backoff_grows_and_is_capped(self) -> None:
        clock = FakeClock()
        provider = FakeProvider(name_value="gigachat", errors=[rate_limited(0.1)] * 3)
        chain = build_chain([provider], clock=clock, total_budget_seconds=1_000.0)
        with self.assertRaises(ProviderRateLimited):
            await complete(chain)
        self.assertEqual(clock.slept, [RATE_LIMIT_BACKOFF_SECONDS, 2 * RATE_LIMIT_BACKOFF_SECONDS])
        runtime = chain._runtimes[0]  # noqa: SLF001 - проверка состояния провайдера
        runtime.rate_limit_streak = 10
        chain._mark_rate_limited(runtime, rate_limited(0.1))  # noqa: SLF001
        self.assertLessEqual(runtime.rate_limited_until - clock.monotonic(), RATE_LIMIT_BACKOFF_MAX_SECONDS)

    async def test_longer_retry_after_is_respected(self) -> None:
        clock = FakeClock()
        provider = FakeProvider(name_value="gigachat", errors=[rate_limited(45.0)])
        await complete(build_chain([provider], clock=clock))
        self.assertEqual(clock.slept, [45.0])

    async def test_window_beyond_budget_reports_rate_limit_honestly(self) -> None:
        clock = FakeClock()
        provider = FakeProvider(name_value="gigachat", errors=[rate_limited(0.1)] * 3)
        chain = build_chain([provider], clock=clock, total_budget_seconds=5.0)
        with self.assertRaises(ProviderRateLimited):
            await complete(chain)
        calls_before = len(provider.calls)
        with self.assertRaises(ProviderRateLimited) as error:
            await complete(chain)
        self.assertEqual(len(provider.calls), calls_before, "в окно ограничения запрос не отправляется")
        self.assertNotIn("ни один провайдер", error.exception.message)

    async def test_success_resets_backoff(self) -> None:
        clock = FakeClock()
        provider = FakeProvider(name_value="gigachat", errors=[rate_limited(0.1)])
        chain = build_chain([provider], clock=clock)
        await complete(chain)
        runtime = chain._runtimes[0]  # noqa: SLF001 - проверка состояния провайдера
        self.assertEqual((runtime.rate_limit_streak, runtime.rate_limited_until), (0, 0.0))

    async def test_real_failures_still_open_circuit(self) -> None:
        clock = FakeClock()
        provider = FakeProvider(name_value="gigachat", errors=[timeout(), timeout(), timeout()])
        chain = build_chain([provider], clock=clock, circuit_breaker_failures=3)
        for _ in range(3):
            with self.assertRaises(ProviderError):
                await complete(chain)
        calls_before = len(provider.calls)
        with self.assertRaises(ProviderError):
            await complete(chain)
        self.assertEqual(len(provider.calls), calls_before, "после настоящих отказов предохранитель разомкнут")


if __name__ == "__main__":
    unittest.main()