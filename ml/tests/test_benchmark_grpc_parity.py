"""Повтор режима А идёт тем же путём, что живые сервисы (этап 2, аудит A3).

Настоящий gRPC-клиент orchestrator (`GrpcInsightClient`) и настоящий сервер insight (`InsightServicer`) соединены
каналом в памяти, который сериализует сообщения так же, как сеть. Тот же прогон отбора через адаптер повтора
(`LoopbackInsight`) обязан дать ту же выдачу, те же вердикты и те же запросы к модели (ключи повтора). Тест выполняется
там, где установлен пакет grpc (образ trainer); без него пропускается.
"""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

HAS_GRPC = importlib.util.find_spec("grpc") is not None


class _Channel:
    """Канал в памяти: сериализует запрос и ответ, как транспорт gRPC, и вызывает метод сервера."""

    def __init__(self, servicer) -> None:  # noqa: ANN001
        self._servicer = servicer

    def unary_unary(self, method, request_serializer=None, response_deserializer=None, **kwargs):  # noqa: ANN001, ANN003, ANN201
        name = method.rsplit("/", 1)[1]

        async def call(request, timeout=None, metadata=None, **options):  # noqa: ANN001, ANN003, ANN202
            payload = request_serializer(request) if request_serializer else request.SerializeToString()
            response = await getattr(self._servicer, name)(type(request).FromString(payload), None)
            raw = response.SerializeToString()
            return response_deserializer(raw) if response_deserializer else response

        return call

    def unary_stream(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None

    stream_unary = stream_stream = unary_stream


@unittest.skipUnless(HAS_GRPC, "нет пакета grpc: проверка выполняется в образе trainer")
class GrpcParityTest(unittest.TestCase):
    def test_loopback_equals_grpc_client_and_server(self) -> None:
        from insight.adapters.inbound.grpc_server import InsightServicer  # noqa: PLC0415
        from insight.application.prompt_builder import PromptBuilder  # noqa: PLC0415
        from insight.application.provider_chain import ChainConfig, ProviderChain  # noqa: PLC0415
        from insight.application.use_cases.finalize_cards import FinalizeCards  # noqa: PLC0415
        from insight.application.use_cases.generate_insight import GenerateConfig, GenerateInsight  # noqa: PLC0415
        from insight.application.use_cases.judge_candidates import JudgeCandidates  # noqa: PLC0415
        from insight.application.use_cases.rubric_judge import RubricJudge  # noqa: PLC0415
        from ml.benchmark.memory import (  # noqa: PLC0415
            LoopbackAnalyzer,
            LoopbackCollector,
            LoopbackInsight,
            MemoryCallLog,
            MemoryInsightRepository,
            MemoryResultRepository,
            ReplayClock,
        )
        from orchestrator.adapters.outbound.grpc.clients import GrpcInsightClient  # noqa: PLC0415
        from orchestrator.application.stages.narrate import NarrateConfig, run_narrate  # noqa: PLC0415

        from tests import test_benchmark_replay as base  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            context = base.analyze(Path(tmp))
        orchestrator_config = base.ROOT / "services/orchestrator/config"
        config = NarrateConfig(top_n=4, selection_mode="rubric", rubric_pool=5, final_check_enabled=True,
                               confidence_calibration=True, signal_model_path=str(orchestrator_config / "signal_classifier.json"),
                               rubric_model_path=str(orchestrator_config / "rubric_ranker.json"), profile_model_path="")
        prompts = PromptBuilder(base.ROOT / "services/insight/prompts", base.ROOT / "services/insight/schemas")

        def use_cases():  # noqa: ANN202
            recorder = base._Recorder()  # noqa: SLF001 - общий помощник тестов
            clock = ReplayClock(base.AS_OF)
            chain = ProviderChain([base.ScriptedLLM()], MemoryCallLog(), clock, ChainConfig(temperature=0.2), recorder=recorder)
            return (JudgeCandidates(chain, prompts), RubricJudge(chain, prompts), FinalizeCards(chain, prompts),
                    GenerateInsight(chain, prompts, MemoryInsightRepository(), clock, GenerateConfig())), recorder

        async def check() -> None:
            return None

        def run(insight) -> MemoryResultRepository:  # noqa: ANN001
            results = MemoryResultRepository()
            asyncio.run(run_narrate(analyzer=LoopbackAnalyzer(context["candidates"]),
                                    collector=LoopbackCollector(context["rows"]), insight=insight, results=results,
                                    job_id=base.JOB_ID, query_text=base.TOPIC, analysis_id=base.ANALYSIS_ID, config=config,
                                    check=check))
            return results

        (judge, rubric, finalize, generate), loop_recorder = use_cases()
        loopback = run(LoopbackInsight(judge, rubric, finalize, generate))
        (judge, rubric, finalize, generate), grpc_recorder = use_cases()
        servicer = InsightServicer(expand_query=None, generate_insight=generate, provider_status=None,
                                   judge_candidates=judge, rubric_judge=rubric, finalize_cards=finalize)
        over_grpc = run(GrpcInsightClient(_Channel(servicer)))

        def cards(results: MemoryResultRepository) -> list:
            return [(i.rank, i.candidate_id, i.title_ru, i.score, i.narrative_status.value,
                     [s.document_id for s in i.sources]) for i in sorted(results.items, key=lambda i: i.rank)]

        self.assertTrue(cards(loopback))
        self.assertEqual(cards(loopback), cards(over_grpc))
        self.assertEqual(loopback.traces["narrate"]["judged"], over_grpc.traces["narrate"]["judged"])
        self.assertEqual([e.replay_key for e in loop_recorder.entries], [e.replay_key for e in grpc_recorder.entries])


if __name__ == "__main__":
    unittest.main()
