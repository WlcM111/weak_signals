"""Цепочка провайдеров LLM: порядок, конкурентность, circuit breaker и суточный бюджет (§6)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from insight.application.dto import (
    CallRecord,
    LLMMessage,
    LLMResult,
    ProviderState,
)
from insight.application.ports import LLMCallLog, LLMProvider, MetricsSink, NullMetrics
from insight.domain.errors import (
    BudgetExhausted,
    ProviderBlocked,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
)
from insight.domain.values import CallStatus, Purpose
from ws_common.clock import Clock
from ws_common.logging import get_logger

MAX_RATE_LIMIT_RETRIES = 2
# Пауза после ответа 429, если провайдер не просит ждать дольше: 10, 20, 40 секунд, не более минуты.
# GigaChat не присылает Retry-After, а окно его ограничения длиннее пяти секунд по умолчанию.
RATE_LIMIT_BACKOFF_SECONDS = 10.0
RATE_LIMIT_BACKOFF_MAX_SECONDS = 60.0
log = get_logger("insight.provider_chain")


@dataclass(slots=True)
class _ProviderRuntime:
    """Состояние одного провайдера: семафор, счётчики и circuit breaker."""

    provider: LLMProvider
    semaphore: asyncio.Semaphore
    failures: int = 0
    unhealthy_until: float = 0.0
    in_flight: int = 0
    queued: int = 0
    last_error: str = ""
    last_success_at: datetime | None = None
    # Ограничение частоты — не поломка: провайдер занят до этого момента, но предохранитель не размыкается.
    rate_limited_until: float = 0.0
    rate_limit_streak: int = 0

    def is_healthy(self, now: float) -> bool:
        """Здоров ли провайдер сейчас (прошёл ли период охлаждения)."""
        return now >= self.unhealthy_until


@dataclass(frozen=True, slots=True)
class ChainConfig:
    """Параметры цепочки провайдеров."""

    timeout_seconds: float = 60.0
    temperature: float = 0.2
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_seconds: float = 60.0
    daily_token_budget: int = 0
    total_budget_seconds: float = 85.0


@dataclass(slots=True)
class ChainOutcome:
    """Результат обращения к цепочке: ответ и провайдер, который его дал."""

    result: LLMResult
    provider: str
    attempts: int = 1


class ProviderChain:
    """Выбирает провайдера по порядку, соблюдая лимиты и отмечая отказы."""

    def __init__(
        self,
        providers: Sequence[LLMProvider],
        call_log: LLMCallLog,
        clock: Clock,
        config: ChainConfig,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._runtimes = [
            _ProviderRuntime(provider=provider, semaphore=asyncio.Semaphore(provider.max_concurrency))
            for provider in providers
        ]
        self._call_log = call_log
        self._clock = clock
        self._config = config
        self._metrics: MetricsSink = metrics or NullMetrics()

    @property
    def is_empty(self) -> bool:
        """Есть ли хоть один настроенный провайдер."""
        return not self._runtimes

    @property
    def active_provider(self) -> str:
        """Первый здоровый провайдер цепочки."""
        now = self._clock.monotonic()
        for runtime in self._runtimes:
            if runtime.is_healthy(now):
                return runtime.provider.name
        return "none"

    def states(self) -> list[ProviderState]:
        """Состояние всех провайдеров для `GetProviderStatus`."""
        now = self._clock.monotonic()
        return [
            ProviderState(
                provider=runtime.provider.name,
                enabled=True,
                healthy=runtime.is_healthy(now),
                max_concurrency=runtime.provider.max_concurrency,
                in_flight=runtime.in_flight,
                queued=runtime.queued,
                last_error=runtime.last_error,
                last_success_at=runtime.last_success_at,
            )
            for runtime in self._runtimes
        ]

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict | None,
        max_tokens: int,
        purpose: Purpose,
        prompt_version: str,
        request_sha256: str,
        idempotency_key: str | None = None,
    ) -> ChainOutcome:
        """Проходит по провайдерам до первого успеха; исчерпание — `ProviderError`.

        Ошибки лимита повторяются на том же провайдере (не более двух раз), тайм-аут и отказ
        переводят на следующего. Каждый вызов, успешный или нет, попадает в журнал.
        """
        await self._check_budget()
        deadline = self._clock.monotonic() + self._config.total_budget_seconds
        last_error: ProviderError | None = None
        for runtime in self._runtimes:
            if not runtime.is_healthy(self._clock.monotonic()):
                continue
            for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 2):
                remaining = deadline - self._clock.monotonic()
                if remaining <= 0:
                    raise last_error or ProviderTimeout("исчерпан общий бюджет времени запроса")
                # Провайдер недавно ответил 429: запрос ждёт конца окна ограничения, а не уходит в него.
                wait = runtime.rate_limited_until - self._clock.monotonic()
                if wait > 0:
                    if wait >= remaining:
                        last_error = last_error or ProviderRateLimited(
                            "провайдер ограничил частоту запросов", runtime.provider.name, wait
                        )
                        break
                    await self._clock.sleep(wait)
                    remaining = deadline - self._clock.monotonic()
                try:
                    result = await self._call(
                        runtime,
                        messages,
                        json_schema=json_schema,
                        max_tokens=max_tokens,
                        purpose=purpose,
                        prompt_version=prompt_version,
                        request_sha256=request_sha256,
                        idempotency_key=idempotency_key,
                        deadline_seconds=min(self._config.timeout_seconds, remaining),
                    )
                except ProviderRateLimited as error:
                    last_error = error
                    if attempt > MAX_RATE_LIMIT_RETRIES:
                        break
                    continue
                except (ProviderTimeout, ProviderBlocked, ProviderError) as error:
                    last_error = error
                    break
                else:
                    return ChainOutcome(result=result, provider=runtime.provider.name, attempts=attempt)
        raise last_error or ProviderError("ни один провайдер LLM не настроен или все недоступны")

    async def _call(
        self,
        runtime: _ProviderRuntime,
        messages: Sequence[LLMMessage],
        *,
        json_schema: dict | None,
        max_tokens: int,
        purpose: Purpose,
        prompt_version: str,
        request_sha256: str,
        idempotency_key: str | None,
        deadline_seconds: float,
    ) -> LLMResult:
        """Один вызов провайдера с учётом семафора, журнала, метрик и circuit breaker."""
        runtime.queued += 1
        async with runtime.semaphore:
            runtime.queued -= 1
            runtime.in_flight += 1
            started = time.perf_counter()
            try:
                result = await runtime.provider.complete(
                    messages,
                    json_schema=json_schema,
                    max_tokens=max_tokens,
                    temperature=self._config.temperature,
                    deadline_seconds=deadline_seconds,
                )
            except ProviderError as error:
                latency = int((time.perf_counter() - started) * 1000)
                await self._record(
                    runtime,
                    purpose,
                    prompt_version,
                    request_sha256,
                    idempotency_key,
                    latency,
                    CallStatus(error.call_status),
                    error.error_code,
                )
                if isinstance(error, ProviderRateLimited):
                    self._mark_rate_limited(runtime, error)
                else:
                    self._mark_failure(runtime, error)
                raise
            finally:
                runtime.in_flight -= 1
        latency = result.latency_ms or int((time.perf_counter() - started) * 1000)
        await self._record(
            runtime,
            purpose,
            prompt_version,
            request_sha256,
            idempotency_key,
            latency,
            CallStatus.OK,
            None,
            result,
        )
        self._mark_success(runtime)
        self._metrics.llm_latency(runtime.provider.name, latency / 1000)
        self._metrics.llm_tokens(runtime.provider.name, "prompt", result.prompt_tokens)
        self._metrics.llm_tokens(runtime.provider.name, "completion", result.completion_tokens)
        return result

    async def _record(
        self,
        runtime: _ProviderRuntime,
        purpose: Purpose,
        prompt_version: str,
        request_sha256: str,
        idempotency_key: str | None,
        latency_ms: int,
        status: CallStatus,
        error_code: str | None,
        result: LLMResult | None = None,
    ) -> None:
        """Пишет метаданные вызова: ни промпт, ни ответ не сохраняются (§14.5 ТЗ)."""
        await self._call_log.record(
            CallRecord(
                purpose=purpose,
                provider=runtime.provider.name,
                model=result.model if result else runtime.provider.model,
                prompt_version=prompt_version,
                request_sha256=request_sha256,
                latency_ms=latency_ms,
                status=status,
                idempotency_key=idempotency_key,
                prompt_tokens=result.prompt_tokens if result else None,
                completion_tokens=result.completion_tokens if result else None,
                error_code=error_code,
            )
        )
        self._metrics.llm_call(runtime.provider.name, status.value)
        log.info(
            "llm.call",
            purpose=purpose.value,
            provider=runtime.provider.name,
            model=result.model if result else runtime.provider.model,
            status=status.value,
            prompt_tokens=result.prompt_tokens if result else None,
            completion_tokens=result.completion_tokens if result else None,
            latency_ms=latency_ms,
        )

    def _mark_failure(self, runtime: _ProviderRuntime, error: ProviderError) -> None:
        """Считает подряд идущие отказы и размыкает цепь при превышении порога."""
        runtime.failures += 1
        runtime.last_error = f"{error.error_code}: {error.message}"[:300]
        if runtime.failures >= self._config.circuit_breaker_failures:
            runtime.unhealthy_until = (
                self._clock.monotonic() + self._config.circuit_breaker_cooldown_seconds
            )
            runtime.failures = 0
            self._metrics.provider_healthy(runtime.provider.name, False)
            log.warning(
                "provider.state",
                provider=runtime.provider.name,
                healthy=False,
                reason=error.error_code,
            )

    def _mark_rate_limited(self, runtime: _ProviderRuntime, error: ProviderRateLimited) -> None:
        """Ответ 429: провайдер занят до конца окна с растущей паузой; предохранитель не трогается.

        Раньше каждый 429 считался отказом провайдера: три ответа 429 одной карточки размыкали
        предохранитель на минуту, и все следующие карточки мгновенно уходили в резерв с причиной
        «ни один провайдер LLM не настроен или все недоступны».
        """
        runtime.rate_limit_streak += 1
        backoff = min(
            RATE_LIMIT_BACKOFF_SECONDS * 2 ** (runtime.rate_limit_streak - 1), RATE_LIMIT_BACKOFF_MAX_SECONDS
        )
        runtime.rate_limited_until = self._clock.monotonic() + max(error.retry_after_seconds, backoff)
        runtime.last_error = f"{error.error_code}: {error.message}"[:300]
        log.warning(
            "provider.rate_limited",
            provider=runtime.provider.name,
            streak=runtime.rate_limit_streak,
            wait_seconds=round(runtime.rate_limited_until - self._clock.monotonic(), 1),
        )

    def _mark_success(self, runtime: _ProviderRuntime) -> None:
        """Успех сбрасывает счётчик отказов и возвращает провайдера в строй."""
        runtime.rate_limit_streak = 0
        runtime.rate_limited_until = 0.0
        runtime.failures = 0
        runtime.last_error = ""
        runtime.last_success_at = self._clock.now()
        runtime.unhealthy_until = 0.0
        self._metrics.provider_healthy(runtime.provider.name, True)

    async def _check_budget(self) -> None:
        """Суточный бюджет токенов: защита от расходов при сбоях (§17 HANDOFF)."""
        if self._config.daily_token_budget <= 0:
            return
        used = await self._call_log.tokens_used_today(self._clock.now())
        self._metrics.daily_tokens(used)
        if used >= self._config.daily_token_budget:
            log.warning("budget.exhausted", used=used, budget=self._config.daily_token_budget)
            raise BudgetExhausted(
                f"израсходован суточный бюджет токенов ({used} ≥ {self._config.daily_token_budget})"
            )
