"""Контрактные тесты `InsightService`: три RPC на детерминированном провайдере.

Запуск: `uv run pytest services/insight/tests/contract -m contract` (нужны сгенерированные стабы:
`bash tools/gen_proto.sh`).
"""

from __future__ import annotations

import json
from concurrent import futures
from pathlib import Path

import grpc
import pytest
import pytest_asyncio
from weaksignals.analyzer.v1 import analyzer_pb2
from weaksignals.common.v1 import common_pb2
from weaksignals.insight.v1 import insight_pb2, insight_pb2_grpc

from insight.adapters.inbound.grpc_server import InsightServicer
from insight.adapters.outbound.llm.fake_provider import FakeProvider, failure, timeout
from insight.application.prompt_builder import PromptBuilder
from insight.application.provider_chain import ChainConfig, ProviderChain
from insight.application.use_cases.expand_query import ExpandQuery, Glossary
from insight.application.use_cases.generate_insight import GenerateConfig, GenerateInsight
from insight.application.use_cases.get_provider_status import GetProviderStatus

from ..fakes import (
    FakeClock,
    InMemoryCallLog,
    InMemoryExpansionRepository,
    InMemoryInsightRepository,
    make_candidate,
    make_evidence,
)

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[4]
PROMPTS = ROOT / "services" / "insight" / "prompts"
SCHEMAS = ROOT / "services" / "insight" / "schemas"
GLOSSARY = ROOT / "services" / "insight" / "config" / "glossary_ru_en.yaml"


def build_servicer(providers, insights=None, clock=None) -> InsightServicer:  # noqa: ANN001
    """Сервисер на дублёрах портов."""
    clock = clock or FakeClock()
    prompts = PromptBuilder(PROMPTS, SCHEMAS)
    chain = ProviderChain(providers, InMemoryCallLog(), clock, ChainConfig())
    return InsightServicer(
        expand_query=ExpandQuery(chain, prompts, InMemoryExpansionRepository(), Glossary.load(GLOSSARY)),
        generate_insight=GenerateInsight(
            chain, prompts, insights or InMemoryInsightRepository(), clock, GenerateConfig()
        ),
        provider_status=GetProviderStatus(chain),
    )


@pytest_asyncio.fixture
async def stub_factory():  # noqa: ANN201 - фикстура pytest
    """Поднимает сервер в процессе и отдаёт фабрику стабов."""
    servers: list[grpc.aio.Server] = []
    channels: list[grpc.aio.Channel] = []

    async def make(servicer: InsightServicer):  # noqa: ANN202
        server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=4))
        insight_pb2_grpc.add_InsightServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        servers.append(server)
        channels.append(channel)
        return insight_pb2_grpc.InsightServiceStub(channel)

    yield make
    for channel in channels:
        await channel.close()
    for server in servers:
        await server.stop(0)


def build_request(key: str = "job-1:cand-1:insight:insight_v1", allow_fallback: bool = True):  # noqa: ANN201
    """Валидный запрос `GenerateInsight`."""
    candidate = make_candidate()
    evidence = [make_evidence(1), make_evidence(2, language="ru")]
    return insight_pb2.GenerateInsightRequest(
        idempotency_key=key,
        candidate=insight_pb2.CandidateContext(
            candidate_id=candidate.candidate_id,
            title=candidate.title,
            keyphrases=list(candidate.keyphrases),
            score=candidate.score,
            decision=analyzer_pb2.DECISION_WEAK_SIGNAL,
            query_text=candidate.query_text,
            top_features=[
                analyzer_pb2.FeatureContribution(
                    feature_name=feature.feature_name,
                    value=feature.value,
                    contribution=feature.contribution,
                    label_ru=feature.label_ru,
                    direction=feature.direction.value,
                )
                for feature in candidate.top_features
            ],
        ),
        evidence=[
            insight_pb2.EvidenceDocument(
                document_id=document.document_id,
                title=document.title,
                url=document.url,
                text=document.text,
                language_code=document.language_code,
                source_type=common_pb2.SOURCE_TYPE_SCIENTIFIC_PUBLICATION,
                trust_level=common_pb2.TRUST_LEVEL_HIGH,
            )
            for document in evidence
        ],
        allow_fallback=allow_fallback,
    )


async def test_generate_insight_returns_grounded_narrative(stub_factory) -> None:  # noqa: ANN001
    """Валидный ответ модели проходит схему и grounding."""
    stub = await stub_factory(build_servicer([FakeProvider()]))
    response = await stub.GenerateInsight(build_request())
    assert response.status == insight_pb2.INSIGHT_STATUS_GENERATED
    assert response.grounding.passed
    assert response.narrative.title_ru
    assert len(response.source_summaries) == 2
    assert response.provenance.provider == "fake"
    assert response.from_cache is False


async def test_invalid_json_then_fallback(stub_factory) -> None:  # noqa: ANN001
    """Невалидный JSON повторяется и приводит к экстрактивному результату."""
    provider = FakeProvider(responses=["не json", "снова не json"])
    stub = await stub_factory(build_servicer([provider]))
    response = await stub.GenerateInsight(build_request(key="job-2:cand-1:insight:insight_v1"))
    assert response.status == insight_pb2.INSIGHT_STATUS_FALLBACK_EXTRACTIVE
    assert len(provider.calls) == 2


async def test_timeout_switches_to_next_provider(stub_factory) -> None:  # noqa: ANN001
    """Тайм-аут основного провайдера переводит вызов на резервный."""
    primary = FakeProvider(name_value="gigachat", errors=[timeout()])
    secondary = FakeProvider(name_value="yandexgpt")
    stub = await stub_factory(build_servicer([primary, secondary]))
    response = await stub.GenerateInsight(build_request(key="job-3:cand-1:insight:insight_v1"))
    assert response.provenance.provider == "yandexgpt"
    assert response.status == insight_pb2.INSIGHT_STATUS_GENERATED


async def test_unavailable_when_fallback_forbidden(stub_factory) -> None:  # noqa: ANN001
    """При запрете резерва и отказе провайдеров возвращается UNAVAILABLE."""
    stub = await stub_factory(build_servicer([FakeProvider(errors=[failure(), failure()])]))
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await stub.GenerateInsight(
            build_request(key="job-4:cand-1:insight:insight_v1", allow_fallback=False)
        )
    assert error.value.code() is grpc.StatusCode.UNAVAILABLE


async def test_repeat_returns_from_cache(stub_factory) -> None:  # noqa: ANN001
    """Повтор с тем же ключом отдаёт результат из кеша без вызова модели."""
    provider = FakeProvider()
    servicer = build_servicer([provider])
    stub = await stub_factory(servicer)
    request = build_request(key="job-5:cand-1:insight:insight_v1")
    first = await stub.GenerateInsight(request)
    calls = len(provider.calls)
    second = await stub.GenerateInsight(request)
    assert second.from_cache is True
    assert second.insight_id == first.insight_id
    assert len(provider.calls) == calls


async def test_invalid_argument_on_empty_evidence(stub_factory) -> None:  # noqa: ANN001
    """Запрос без доказательств отклоняется как некорректный."""
    stub = await stub_factory(build_servicer([FakeProvider()]))
    request = build_request(key="job-6:cand-1:insight:insight_v1")
    del request.evidence[:]
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await stub.GenerateInsight(request)
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


async def test_expand_query(stub_factory) -> None:  # noqa: ANN001
    """ExpandQuery отдаёт термины обоих языков."""
    provider = FakeProvider(
        responses=[
            json.dumps(
                {
                    "ru_terms": ["нейроморфные чипы"],
                    "en_terms": ["neuromorphic chips"],
                    "domain_tags": ["edge"],
                },
                ensure_ascii=False,
            )
        ]
    )
    stub = await stub_factory(build_servicer([provider]))
    response = await stub.ExpandQuery(insight_pb2.ExpandQueryRequest(query_text="нейроморфные чипы"))
    assert list(response.ru_terms) == ["нейроморфные чипы"]
    assert list(response.en_terms) == ["neuromorphic chips"]
    assert response.used_fallback is False


async def test_expand_query_fallback_without_providers(stub_factory) -> None:  # noqa: ANN001
    """Без провайдеров расширение собирается по глоссарию."""
    stub = await stub_factory(build_servicer([]))
    response = await stub.ExpandQuery(
        insight_pb2.ExpandQueryRequest(query_text="слабые сигналы в кибербезопасности")
    )
    assert response.used_fallback is True
    assert response.ru_terms


async def test_provider_status(stub_factory) -> None:  # noqa: ANN001
    """GetProviderStatus отражает конкурентность провайдера."""
    stub = await stub_factory(build_servicer([FakeProvider(name_value="gigachat", concurrency=1)]))
    response = await stub.GetProviderStatus(insight_pb2.GetProviderStatusRequest())
    assert response.active_provider == "gigachat"
    assert response.providers[0].max_concurrency == 1
