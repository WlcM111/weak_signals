"""CLI проверки здоровья для Docker healthcheck: `python -m ws_common.healthcheck grpc|http <target>`."""

from __future__ import annotations

import sys
import urllib.request

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc


def check_grpc(target: str, timeout: float = 4.0) -> bool:
    """Проверяет grpc.health.v1 сервиса: SERVING → True."""
    with grpc.insecure_channel(target) as channel:
        stub = health_pb2_grpc.HealthStub(channel)
        try:
            response = stub.Check(health_pb2.HealthCheckRequest(service=""), timeout=timeout)
        except grpc.RpcError:
            return False
    return bool(response.status == health_pb2.HealthCheckResponse.SERVING)


def check_http(url: str, timeout: float = 4.0) -> bool:
    """Проверяет HTTP-эндпоинт: код 200 → True."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - адрес из compose
            return bool(response.status == 200)
    except OSError:
        return False


def main(argv: list[str]) -> int:
    """Точка входа CLI."""
    if len(argv) != 3 or argv[1] not in {"grpc", "http"}:
        print("использование: python -m ws_common.healthcheck grpc|http <target>", file=sys.stderr)
        return 2
    ok = check_grpc(argv[2]) if argv[1] == "grpc" else check_http(argv[2])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
