"""Повтор анализа на сохранённых коллекциях с бо́льшим числом кандидатов (опыт R-012/R-020: где теряются эталонные
сигналы до 150 кандидатов). Запуск ВНУТРИ контейнера analyzer; сеть к источникам и LLM не нужна. Каждый повтор —
новый анализ с ключом `replay-<job_id>-<N>`: исходные задания и их результаты не меняются.

Вход (stdin, TSV без заголовка): job_id, collection_id, query_text. Выход (stdout, TSV): job_id, replay_analysis_id,
статус. Пример: ... | docker compose exec -T analyzer python /tmp/replay_preselect.py 500
"""

from __future__ import annotations

import sys
import time
import uuid

import grpc

from weaksignals.analyzer.v1 import analyzer_pb2, analyzer_pb2_grpc
from weaksignals.common.v1 import common_pb2

ADDRESS = "localhost:50052"
TERMINAL = {"OPERATION_STATUS_COMPLETED", "OPERATION_STATUS_PARTIAL", "OPERATION_STATUS_FAILED",
            "OPERATION_STATUS_CANCELLED"}
TIMEOUT_SECONDS = 4 * 3600


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    rows = [line.rstrip("\n").split("\t") for line in sys.stdin if line.strip()]
    metadata = (("x-correlation-id", str(uuid.uuid4())), ("x-caller", "replay-preselect"))
    failures = 0
    with grpc.insecure_channel(ADDRESS) as channel:
        stub = analyzer_pb2_grpc.AnalyzerServiceStub(channel)
        for job_id, collection_id, query_text in rows:
            started = stub.StartAnalysis(analyzer_pb2.StartAnalysisRequest(
                idempotency_key=f"replay-{job_id}-{limit}", collection_id=collection_id, query_text=query_text,
                params=analyzer_pb2.AnalysisParams(max_candidates=limit)), timeout=60, metadata=metadata)
            until = time.monotonic() + TIMEOUT_SECONDS
            status = common_pb2.OperationStatus.Name(started.status)
            while status not in TERMINAL and time.monotonic() < until:
                time.sleep(20)
                view = stub.GetAnalysis(analyzer_pb2.GetAnalysisRequest(analysis_id=started.analysis_id), timeout=60,
                                        metadata=metadata)
                status = common_pb2.OperationStatus.Name(view.status)
            failures += status not in ("OPERATION_STATUS_COMPLETED", "OPERATION_STATUS_PARTIAL")
            print(f"{job_id}\t{started.analysis_id}\t{status}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
