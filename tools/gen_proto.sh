#!/usr/bin/env bash
# Генерация стабов контрактов из нормативных .proto (§15.5 ARCHITECTURE_TZ.md).
# Результат — пакет `weaksignals.*` внутри libs/ws_contracts/src (внутренние импорты
# сгенерированных модулей разрешаются относительно этого корня).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=libs/ws_contracts/src
mkdir -p "$OUT"
uv run python -m grpc_tools.protoc -I proto \
  --python_out="$OUT" --pyi_out="$OUT" --grpc_python_out="$OUT" \
  proto/weaksignals/*/v1/*.proto
find "$OUT/weaksignals" -type d -exec sh -c 'test -f "$1/__init__.py" || : > "$1/__init__.py"' _ {} \;
echo "стабы сгенерированы в $OUT"
