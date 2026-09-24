"""Сценарий GetProviderStatus: состояние провайдеров LLM для readiness и интерфейса."""

from __future__ import annotations

from insight.application.dto import ProviderState
from insight.application.provider_chain import ProviderChain


class GetProviderStatus:
    """Отдаёт состояние всех провайдеров и активного из них."""

    def __init__(self, chain: ProviderChain) -> None:
        self._chain = chain

    def execute(self) -> tuple[list[ProviderState], str]:
        """Список состояний и имя активного провайдера (`none`, если все недоступны)."""
        return self._chain.states(), self._chain.active_provider


class RegisterPrompts:
    """Регистрирует версии промптов в БД при старте сервиса (§7 HANDOFF)."""

    def __init__(self, registry, prompts) -> None:  # noqa: ANN001 - PromptRegistry, PromptBuilder
        self._registry = registry
        self._prompts = prompts

    async def execute(self) -> int:
        """Записывает версии и возвращает их число."""
        definitions = self._prompts.definitions()
        await self._registry.register(definitions)
        return len(definitions)
