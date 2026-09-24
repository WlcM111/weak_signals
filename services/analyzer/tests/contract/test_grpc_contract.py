"""Контрактные тесты AnalyzerService: реальный gRPC-сервер в процессе.

Запуск: `uv run pytest services/analyzer/tests/contract -m contract` (нужны сгенерированные стабы:
`bash tools/gen_proto.sh`).
"""

from __future__ import annotations

import tempfile
from concurrent import futures
from pathlib import Path

import grpc
import pytest
from weaksignals.analyzer.v1 import analyzer_pb2, analyzer_pb2_grpc
from weaksignals.common.v1 import common_pb2

from analyzer.adapters.inbound.grpc_server import AnalyzerServicer
from analyzer.adapters.outbound.config_loader import load_feature_registry, load_lexicons
from analyzer.adapters.outbound.model_store import FileSystemModelStore
from analyzer.application.active_model import ActiveModelHolder
from analyzer.application.use_cases.cancel_analysis import CancelAnalysis
from analyzer.application.use_cases.get_analysis import GetAnalysis
from analyzer.application.use_cases.get_model_info import GetModelInfo
from analyzer.application.use_cases.list_candidates import ListCandidates
from analyzer.application.use_cases.score_text import ScoreText
from analyzer.application.use_cases.start_analysis import StartAnalysis
from ..fakes import (
    FakeClock,
    FakeCollector,
    FakeEmbedder,
    InMemoryAnalysisRepository,
    InMemoryCandidateRepository,
    make_document,
)
from ..model_fixture import MODEL_VERSION_ID, build_model_store

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[4]
COLLECTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UNKNOWN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


@pytest.fixture(scope="module")
def stub():  # noqa: ANN201 - фикстура pytest
    """Поднимает сервер с дублёрами портов и возвращает клиентский стаб."""
    registry = load_feature_registry(REPO_ROOT / "schemas" / "feature_registry_v1.json")
    lexicons = load_lexicons(
        REPO_ROOT / "services" / "analyzer" / "config" / "lexicons",
        REPO_ROOT / "services" / "analyzer" / "config" / "stage_rules.yaml",
    )
    with tempfile.TemporaryDirectory() as tmp:
        build_model_store(Path(tmp), registry.model_names)
        bundle = FileSystemModelStore(Path(tmp)).load_active(registry)
        holder = ActiveModelHolder(bundle)
        clock = FakeClock()
        analyses = InMemoryAnalysisRepository(clock)
        candidates = InMemoryCandidateRepository(analyses, clock)
        collector = FakeCollector(documents=[make_document(1, "документ")])
        servicer = AnalyzerServicer(
            start_analysis=StartAnalysis(analyses, collector, holder, 20),
            get_analysis=GetAnalysis(analyses),
            list_candidates=ListCandidates(analyses, candidates),
            cancel_analysis=CancelAnalysis(analyses),
            score_text=ScoreText(collector, FakeEmbedder(), holder, lexicons, clock),
            get_model_info=GetModelInfo(holder),
        )
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        analyzer_pb2_grpc.add_AnalyzerServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        yield analyzer_pb2_grpc.AnalyzerServiceStub(channel)
        channel.close()
        server.stop(0).wait(5)


def start_request(key: str = "job-1:analyze") -> analyzer_pb2.StartAnalysisRequest:
    """Валидный запрос запуска анализа."""
    return analyzer_pb2.StartAnalysisRequest(
        idempotency_key=key, collection_id=COLLECTION_ID, query_text="нейроморфные вычисления"
    )


def test_start_analysis_returns_pending(stub) -> None:  # noqa: ANN001 - фикстура
    """StartAnalysis принимает запрос и отдаёт активную версию модели."""
    response = stub.StartAnalysis(start_request())
    assert response.status == common_pb2.OPERATION_STATUS_PENDING
    assert response.model_version_id == MODEL_VERSION_ID
    assert response.already_existed is False


def test_start_analysis_is_idempotent(stub) -> None:  # noqa: ANN001 - фикстура
    """Повтор с тем же ключом возвращает тот же анализ."""
    first = stub.StartAnalysis(start_request("job-2:analyze"))
    second = stub.StartAnalysis(start_request("job-2:analyze"))
    assert second.already_existed is True
    assert first.analysis_id == second.analysis_id


def test_validation_errors(stub) -> None:  # noqa: ANN001 - фикстура
    """Некорректные поля отклоняются с INVALID_ARGUMENT."""
    with pytest.raises(grpc.RpcError) as error:
        stub.StartAnalysis(analyzer_pb2.StartAnalysisRequest(idempotency_key="x", query_text="a"))
    assert error.value.code() is grpc.StatusCode.INVALID_ARGUMENT


def test_get_analysis_not_found(stub) -> None:  # noqa: ANN001 - фикстура
    """Неизвестный анализ — NOT_FOUND."""
    with pytest.raises(grpc.RpcError) as error:
        stub.GetAnalysis(analyzer_pb2.GetAnalysisRequest(analysis_id=UNKNOWN_ID))
    assert error.value.code() is grpc.StatusCode.NOT_FOUND


def test_cancel_is_idempotent(stub) -> None:  # noqa: ANN001 - фикстура
    """Повторная отмена не ошибка."""
    analysis_id = stub.StartAnalysis(start_request("job-3:analyze")).analysis_id
    first = stub.CancelAnalysis(analyzer_pb2.CancelAnalysisRequest(analysis_id=analysis_id))
    second = stub.CancelAnalysis(analyzer_pb2.CancelAnalysisRequest(analysis_id=analysis_id))
    assert first.status == second.status


def test_list_candidates_pagination(stub) -> None:  # noqa: ANN001 - фикстура
    """Пустой анализ отдаёт пустую страницу без токена."""
    analysis_id = stub.StartAnalysis(start_request("job-4:analyze")).analysis_id
    response = stub.ListCandidates(
        analyzer_pb2.ListCandidatesRequest(analysis_id=analysis_id, include_excluded=True)
    )
    assert list(response.candidates) == []
    assert response.page.next_page_token == ""


def test_score_text_returns_full_feature_vector(stub) -> None:  # noqa: ANN001 - фикстура
    """ScoreText отдаёт 25 признаков и версию модели."""
    response = stub.ScoreText(
        analyzer_pb2.ScoreTextRequest(title="Нейроморфные чипы", description="прототип")
    )
    assert len(response.features) == 25
    assert response.model_version_id == MODEL_VERSION_ID
    assert response.enrichment_applied is False


def test_get_model_info(stub) -> None:  # noqa: ANN001 - фикстура
    """GetModelInfo отдаёт метрики и порядок признаков реестра."""
    response = stub.GetModelInfo(analyzer_pb2.GetModelInfoRequest())
    assert response.feature_schema_version == "v1"
    assert len(response.feature_names) == 25
    assert 0.0 < response.metrics.threshold < 1.0


def test_precondition_without_model() -> None:
    """Без активной модели StartAnalysis отвечает FAILED_PRECONDITION `MODEL_NOT_LOADED`."""
    registry = load_feature_registry(REPO_ROOT / "schemas" / "feature_registry_v1.json")
    lexicons = load_lexicons(
        REPO_ROOT / "services" / "analyzer" / "config" / "lexicons",
        REPO_ROOT / "services" / "analyzer" / "config" / "stage_rules.yaml",
    )
    clock = FakeClock()
    analyses = InMemoryAnalysisRepository(clock)
    holder = ActiveModelHolder()
    collector = FakeCollector(documents=[make_document(1, "документ")])
    servicer = AnalyzerServicer(
        start_analysis=StartAnalysis(analyses, collector, holder, 20),
        get_analysis=GetAnalysis(analyses),
        list_candidates=ListCandidates(analyses, InMemoryCandidateRepository(analyses, clock)),
        cancel_analysis=CancelAnalysis(analyses),
        score_text=ScoreText(collector, FakeEmbedder(), holder, lexicons, clock),
        get_model_info=GetModelInfo(holder),
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    analyzer_pb2_grpc.add_AnalyzerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        client = analyzer_pb2_grpc.AnalyzerServiceStub(channel)
        with pytest.raises(grpc.RpcError) as error:
            client.StartAnalysis(start_request("job-5:analyze"))
        assert error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    finally:
        channel.close()
        server.stop(0).wait(5)
