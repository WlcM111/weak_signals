#!/usr/bin/env python3
"""Статическая проверка соответствия кода нормативным .proto (без protoc).

Проверяет три вещи, которые иначе выявляются только запуском gRPC:
1. Все поля, передаваемые в конструкторы сообщений protobuf, существуют в контракте.
2. Перечисления домена и proto совпадают по составу (с учётом префикса имени).
3. Набор RPC сервиса совпадает с методами servicer-а.

Запуск: python tools/check_proto_conformance.py
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTO_FILES = sorted((ROOT / "proto").rglob("*.proto"))


@dataclass(frozen=True)
class ServiceSpec:
    """Описание проверяемого сервиса: код транспорта, перечисления домена и имя сервиса."""

    package: str
    service: str
    servicer: str
    enum_prefixes: dict[str, str]
    enum_values_allowed_to_miss: frozenset[str] = frozenset()
    client_files: tuple[str, ...] = ()  # для сервисов-клиентов: файлы, где строятся сообщения

    @property
    def source_root(self) -> Path:
        """Каталог `src` сервиса."""
        return ROOT / "services" / self.package / "src"

    @property
    def code_files(self) -> list[Path]:
        """Файлы, где строятся сообщения protobuf: транспорт сервера или клиенты."""
        if self.client_files:
            return [self.source_root / self.package / name for name in self.client_files]
        base = self.source_root / self.package / "adapters" / "inbound"
        return [base / "mappers.py", base / "grpc_server.py"]


SERVICES = (
    ServiceSpec(
        package="orchestrator",
        service="",  # orchestrator не реализует gRPC-сервис: он только клиент трёх сервисов
        servicer="",
        enum_prefixes={"Decision": "DECISION_", "TrustLevel": "TRUST_LEVEL_"},
        enum_values_allowed_to_miss=frozenset(),
        client_files=("adapters/outbound/grpc/clients.py", "adapters/outbound/grpc/mappers.py"),
    ),
    ServiceSpec(
        package="insight",
        service="InsightService",
        servicer="InsightServicer",
        enum_prefixes={
            "InsightStatus": "INSIGHT_STATUS_",
            "SummaryKind": "SUMMARY_KIND_",
            "Decision": "DECISION_",
            "TrustLevel": "TRUST_LEVEL_",
        },
    ),
    ServiceSpec(
        package="collector",
        service="CollectorService",
        servicer="CollectorServicer",
        enum_prefixes={
            "SourceKey": "SOURCE_KEY_",
            "SourceType": "SOURCE_TYPE_",
            "TrustLevel": "TRUST_LEVEL_",
            "OperationStatus": "OPERATION_STATUS_",
            "CollectionMode": "COLLECTION_MODE_",
        },
    ),
    ServiceSpec(
        package="analyzer",
        service="AnalyzerService",
        servicer="AnalyzerServicer",
        enum_prefixes={
            "SourceType": "SOURCE_TYPE_",
            "TrustLevel": "TRUST_LEVEL_",
            "OperationStatus": "OPERATION_STATUS_",
            "Decision": "DECISION_",
            "DecisionReason": "DECISION_REASON_",
        },
        # анализ не бывает частичным: PARTIAL относится только к коллекциям collector-а
        enum_values_allowed_to_miss=frozenset({"OPERATION_STATUS_PARTIAL"}),
    ),
)

_MESSAGE_RE = re.compile(r"^\s*message\s+(\w+)\s*\{", re.MULTILINE)
_ENUM_RE = re.compile(r"^\s*enum\s+(\w+)\s*\{", re.MULTILINE)
_SERVICE_RE = re.compile(r"^\s*service\s+(\w+)\s*\{", re.MULTILINE)
_FIELD_RE = re.compile(r"^\s*(?:repeated\s+|optional\s+)?[\w.]+\s+(\w+)\s*=\s*(\d+)\s*;", re.MULTILINE)
_ENUM_VALUE_RE = re.compile(r"^\s*(\w+)\s*=\s*(\d+)\s*;", re.MULTILINE)
_RPC_RE = re.compile(r"^\s*rpc\s+(\w+)\s*\(", re.MULTILINE)


def parse_blocks(text: str, header: re.Pattern[str]) -> dict[str, str]:
    """Возвращает тело каждого блока (message/enum/service) по его имени."""
    blocks: dict[str, str] = {}
    for match in header.finditer(text):
        depth, start = 0, match.end() - 1
        for index in range(start, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    blocks[match.group(1)] = text[start + 1 : index]
                    break
    return blocks


def load_contracts() -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """Сообщения → поля, перечисления → значения, сервисы → RPC."""
    messages: dict[str, set[str]] = {}
    enums: dict[str, set[str]] = {}
    services: dict[str, set[str]] = {}
    for path in PROTO_FILES:
        text = path.read_text(encoding="utf-8")
        for name, body in parse_blocks(text, _MESSAGE_RE).items():
            messages[name] = {field.group(1) for field in _FIELD_RE.finditer(body)}
        for name, body in parse_blocks(text, _ENUM_RE).items():
            enums[name] = {value.group(1) for value in _ENUM_VALUE_RE.finditer(body)}
        for name, body in parse_blocks(text, _SERVICE_RE).items():
            services[name] = {rpc.group(1) for rpc in _RPC_RE.finditer(body)}
    return messages, enums, services


def check_message_kwargs(messages: dict[str, set[str]], service: ServiceSpec) -> list[str]:
    """Каждый kwarg конструктора `*_pb2.Message(...)` должен быть полем контракта."""
    problems: list[str] = []
    for path in service.code_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            module = node.func.value
            if not isinstance(module, ast.Name) or not module.id.endswith("_pb2"):
                continue
            message = node.func.attr
            if message not in messages:
                continue
            for keyword in node.keywords:
                if keyword.arg and keyword.arg not in messages[message]:
                    problems.append(
                        f"{path.name}:{node.lineno}: поля {keyword.arg!r} нет в сообщении {message}"
                    )
    return problems


def check_enums(enums: dict[str, set[str]], service: ServiceSpec) -> list[str]:
    """Состав перечислений домена и proto должен совпадать (с учётом префикса)."""
    import importlib  # noqa: PLC0415 - импорт рядом с использованием

    for path in (service.source_root, ROOT / "libs/ws_common/src"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    domain_values = importlib.import_module(f"{service.package}.domain.values")

    problems: list[str] = []
    for enum_name, prefix in service.enum_prefixes.items():
        proto_values = enums.get(enum_name)
        if proto_values is None:
            problems.append(f"в контрактах нет перечисления {enum_name}")
            continue
        domain_members = {member.name for member in getattr(domain_values, enum_name)}
        expected = {f"{prefix}{name}" for name in domain_members}
        missing_in_proto = expected - proto_values
        missing_in_domain = (
            proto_values - expected - {f"{prefix}UNSPECIFIED"} - service.enum_values_allowed_to_miss
        )
        problems.extend(
            f"{service.package}.{enum_name}: значения {name} нет в .proto"
            for name in sorted(missing_in_proto)
        )
        problems.extend(
            f"{service.package}.{enum_name}: значение {name} из .proto отсутствует в домене"
            for name in sorted(missing_in_domain)
        )
    return problems


def check_rpcs(services: dict[str, set[str]], service: ServiceSpec) -> list[str]:
    """Методы servicer-а должны покрывать все RPC сервиса."""
    path = service.code_files[1]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    implemented = {
        item.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == service.servicer
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and not item.name.startswith("_")
    }
    declared = services.get(service.service, set())
    problems = [
        f"RPC {name} не реализован в {service.servicer}" for name in sorted(declared - implemented)
    ]
    problems.extend(
        f"метод {name} не объявлен в контракте {service.service}"
        for name in sorted(implemented - declared)
    )
    return problems


def main() -> int:
    """Запускает все проверки для каждого реализованного сервиса и печатает итог."""
    messages, enums, services = load_contracts()
    problems: list[str] = []
    for service in SERVICES:
        if not service.code_files[0].is_file():
            continue
        problems += check_message_kwargs(messages, service)
        problems += check_enums(enums, service)
        if service.service:
            problems += check_rpcs(services, service)
        print(
            f"{service.package}: RPC {len(services.get(service.service, ()))}, "
            f"перечислений {len(service.enum_prefixes)}"
        )
    print(f"контракты: сообщений {len(messages)}, перечислений {len(enums)}")
    for problem in problems:
        print(f"ОШИБКА: {problem}")
    print("RESULT: PASS" if not problems else f"RESULT: FAIL ({len(problems)})")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
