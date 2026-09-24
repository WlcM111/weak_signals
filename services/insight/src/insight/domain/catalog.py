"""Реестр провайдеров и моделей, разрешённых ТЗ кейса (§3.1 «Допустимые облачные API»).

Реестр — единственный источник правды о том, какую модель можно выбрать, каким протоколом с ней
говорить и что она умеет. Он не выводится из строки конфигурации и не пополняется на лету:
модель вне списка ТЗ выбрать нельзя ни через окружение, ни через интерфейс.

Разделение протоколов принципиально: у GigaChat собственная авторизация (OAuth по Authorization
Key с последующим Bearer-токеном) и собственный SDK, у YandexGPT — OpenAI-совместимый путь с
заголовком `Api-Key` и именем модели вида `gpt://<каталог>/<модель>`, у остальных —
классический OpenAI-совместимый протокол. Считать, что провайдеры отличаются только base URL,
неверно, поэтому у каждого семейства свой адаптер и своя карта возможностей.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Protocol(StrEnum):
    """Семейство протокола, определяющее адаптер."""

    GIGACHAT_SDK = "gigachat_sdk"
    OPENAI_CHAT = "openai_chat"
    YANDEX_OPENAI = "yandex_openai"
    LOCAL_OPENAI = "local_openai"
    FAKE = "fake"


class Capability(StrEnum):
    """Возможности, которые сервис реально использует или проверяет."""

    CHAT = "chat"
    JSON_MODE = "json_mode"
    STREAMING = "streaming"
    TOOLS = "tools"
    TOKEN_USAGE = "token_usage"
    SYSTEM_ROLE = "system_role"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Одна модель реестра."""

    model_id: str
    title_ru: str
    provider: str
    protocol: Protocol
    context_tokens: int
    max_output_tokens: int
    capabilities: frozenset[Capability]
    notes_ru: str = ""

    def supports(self, capability: Capability) -> bool:
        """Поддерживает ли модель возможность."""
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Компания-провайдер и общие для её моделей свойства."""

    provider_id: str
    company_ru: str
    protocol: Protocol
    default_base_url: str
    auth_hint_ru: str
    credential_env: str
    extra_env: tuple[str, ...] = ()
    allows_custom_endpoint: bool = False
    models: tuple[ModelSpec, ...] = field(default_factory=tuple)

    @property
    def model_ids(self) -> tuple[str, ...]:
        """Идентификаторы моделей провайдера."""
        return tuple(model.model_id for model in self.models)


_CHAT_JSON = frozenset(
    {Capability.CHAT, Capability.JSON_MODE, Capability.TOKEN_USAGE, Capability.SYSTEM_ROLE}
)
_CHAT_PLAIN = frozenset({Capability.CHAT, Capability.TOKEN_USAGE, Capability.SYSTEM_ROLE})

GIGACHAT = ProviderSpec(
    provider_id="gigachat",
    company_ru="Сбер · GigaChat",
    protocol=Protocol.GIGACHAT_SDK,
    default_base_url="https://gigachat.devices.sberbank.ru/api/v1",
    auth_hint_ru="Authorization Key из кабинета developers.sber.ru; сервис сам меняет его на токен",
    credential_env="WS_GIGACHAT_CREDENTIALS",
    extra_env=("WS_GIGACHAT_SCOPE", "WS_GIGACHAT_MODEL", "WS_GIGACHAT_MAX_CONCURRENCY"),
    models=(
        ModelSpec("GigaChat-2", "GigaChat 2 Lite", "gigachat", Protocol.GIGACHAT_SDK,
                  131_072, 2_048, _CHAT_PLAIN, "Лёгкая модель, быстрее и дешевле"),
        ModelSpec("GigaChat-2-Pro", "GigaChat 2 Pro", "gigachat", Protocol.GIGACHAT_SDK,
                  131_072, 2_048, _CHAT_PLAIN, "Основной вариант для нарративов"),
        ModelSpec("GigaChat-2-Max", "GigaChat 2 Max", "gigachat", Protocol.GIGACHAT_SDK,
                  131_072, 2_048, _CHAT_PLAIN, "Самая сильная, самая дорогая"),
    ),
)

YANDEXGPT = ProviderSpec(
    provider_id="yandexgpt",
    company_ru="Яндекс · YandexGPT",
    protocol=Protocol.YANDEX_OPENAI,
    default_base_url="https://llm.api.cloud.yandex.net/v1",
    auth_hint_ru="API-ключ сервисного аккаунта с ролью ai.languageModels.user и идентификатор каталога",
    credential_env="WS_YANDEX_API_KEY",
    extra_env=("WS_YANDEX_FOLDER_ID", "WS_YANDEX_MODEL"),
    models=(
        ModelSpec("yandexgpt-lite/latest", "YandexGPT Lite 5", "yandexgpt", Protocol.YANDEX_OPENAI,
                  32_000, 2_000, _CHAT_JSON),
        ModelSpec("yandexgpt/latest", "YandexGPT Pro 5", "yandexgpt", Protocol.YANDEX_OPENAI,
                  32_000, 2_000, _CHAT_JSON),
        ModelSpec("yandexgpt/rc", "YandexGPT Pro 5.1", "yandexgpt", Protocol.YANDEX_OPENAI,
                  32_000, 2_000, _CHAT_JSON, "Release candidate, поведение может меняться"),
    ),
)

QWEN = ProviderSpec(
    provider_id="qwen",
    company_ru="Qwen (OpenAI-совместимый endpoint)",
    protocol=Protocol.OPENAI_CHAT,
    default_base_url="",
    auth_hint_ru="Ключ и адрес endpoint задаёт администратор: ТЗ не фиксирует площадку размещения",
    credential_env="WS_QWEN_API_KEY",
    extra_env=("WS_QWEN_BASE_URL", "WS_QWEN_MODEL"),
    allows_custom_endpoint=True,
    models=(
        ModelSpec("qwen3.6-35b-a3b", "Qwen3.6 35B-A3B", "qwen", Protocol.OPENAI_CHAT,
                  128_000, 4_096, _CHAT_JSON, "MoE: 35B всего, ~3B активных"),
        ModelSpec("qwen3-235b", "Qwen3 235B", "qwen", Protocol.OPENAI_CHAT,
                  128_000, 4_096, _CHAT_JSON, "MoE большого размера"),
    ),
)

OPENAI = ProviderSpec(
    provider_id="openai",
    company_ru="OpenAI",
    protocol=Protocol.OPENAI_CHAT,
    default_base_url="https://api.openai.com/v1",
    auth_hint_ru="Ключ проекта OpenAI в заголовке Authorization: Bearer",
    credential_env="WS_OPENAI_API_KEY",
    extra_env=("WS_OPENAI_BASE_URL", "WS_OPENAI_MODEL"),
    allows_custom_endpoint=True,
    models=(
        ModelSpec("gpt-4.1", "GPT-4.1", "openai", Protocol.OPENAI_CHAT,
                  128_000, 4_096, _CHAT_JSON | {Capability.STREAMING, Capability.TOOLS}),
        ModelSpec("gpt-5.6-luna", "GPT-5.6 Luna", "openai", Protocol.OPENAI_CHAT,
                  128_000, 4_096, _CHAT_JSON | {Capability.STREAMING, Capability.TOOLS}),
    ),
)

LOCAL = ProviderSpec(
    provider_id="local_llamacpp",
    company_ru="Локальная модель (llama.cpp, профиль compose local-llm)",
    protocol=Protocol.LOCAL_OPENAI,
    default_base_url="http://local-llm:8000/v1",
    auth_hint_ru="Без ключа: сервис поднимается во внутренней сети compose",
    credential_env="",
    extra_env=("WS_LOCAL_LLM_BASE_URL", "WS_LOCAL_LLM_GGUF"),
    allows_custom_endpoint=True,
    models=(
        ModelSpec("local-model", "Локальная GGUF-модель", "local_llamacpp", Protocol.LOCAL_OPENAI,
                  32_768, 2_048, _CHAT_PLAIN, "Конкретные веса задаёт WS_LOCAL_LLM_GGUF"),
    ),
)

FAKE = ProviderSpec(
    provider_id="fake",
    company_ru="Детерминированный дублёр (только WS_ENV=test)",
    protocol=Protocol.FAKE,
    default_base_url="",
    auth_hint_ru="Не требует ключа; в рабочем окружении запрещён",
    credential_env="",
    models=(
        ModelSpec("fake-deterministic-1", "Дублёр", "fake", Protocol.FAKE, 8_192, 1_024, _CHAT_JSON),
    ),
)

PROVIDERS: tuple[ProviderSpec, ...] = (GIGACHAT, YANDEXGPT, QWEN, OPENAI, LOCAL, FAKE)
BY_ID: dict[str, ProviderSpec] = {provider.provider_id: provider for provider in PROVIDERS}
# Провайдеры, которые ТЗ разрешает без отдельного согласования с заказчиком.
APPROVED_PROVIDER_IDS: frozenset[str] = frozenset(
    {"gigachat", "yandexgpt", "qwen", "openai", "local_llamacpp", "none"}
)


def provider(provider_id: str) -> ProviderSpec | None:
    """Описание провайдера по идентификатору."""
    return BY_ID.get(provider_id)


def model(provider_id: str, model_id: str) -> ModelSpec | None:
    """Описание модели, если она есть у этого провайдера."""
    spec = BY_ID.get(provider_id)
    if spec is None:
        return None
    for candidate in spec.models:
        if candidate.model_id == model_id:
            return candidate
    return None


def is_allowed(provider_id: str, model_id: str) -> bool:
    """Разрешена ли пара «провайдер + модель» техническим заданием."""
    return provider_id in APPROVED_PROVIDER_IDS and model(provider_id, model_id) is not None


def catalog_rows() -> list[dict[str, object]]:
    """Плоское представление реестра для интерфейса и отчётов."""
    rows: list[dict[str, object]] = []
    for spec in PROVIDERS:
        for item in spec.models:
            rows.append(
                {
                    "provider_id": spec.provider_id,
                    "company_ru": spec.company_ru,
                    "protocol": item.protocol.value,
                    "model_id": item.model_id,
                    "title_ru": item.title_ru,
                    "context_tokens": item.context_tokens,
                    "max_output_tokens": item.max_output_tokens,
                    "capabilities": sorted(capability.value for capability in item.capabilities),
                    "credential_env": spec.credential_env,
                    "custom_endpoint": spec.allows_custom_endpoint,
                    "notes_ru": item.notes_ru,
                }
            )
    return rows