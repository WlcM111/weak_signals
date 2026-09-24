# Образ orchestrator: два процесса из одного пакета — `api` (FastAPI) и `worker` (цикл заданий).
# Контекст сборки — корень репозитория (compose.yaml: context ".", dockerfile "services/orchestrator/Dockerfile").
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app

FROM base AS deps
# uv.lock копируется, если он есть в репозитории (создаётся командой `uv lock`): сборка становится воспроизводимой.
COPY pyproject.toml uv.lock* ./
# Все pyproject членов рабочего пространства: без них uv не соберёт граф зависимостей.
COPY libs/ws_common/pyproject.toml libs/ws_common/pyproject.toml
COPY libs/ws_contracts/pyproject.toml libs/ws_contracts/pyproject.toml
COPY services/collector/pyproject.toml services/collector/pyproject.toml
COPY services/analyzer/pyproject.toml services/analyzer/pyproject.toml
COPY services/orchestrator/pyproject.toml services/orchestrator/pyproject.toml
COPY services/insight/pyproject.toml services/insight/pyproject.toml
COPY ml/pyproject.toml ml/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
uv sync --no-dev --package orchestrator --no-install-workspace $([ -f uv.lock ] && echo --frozen)

FROM deps AS build-contracts
COPY proto proto
COPY libs libs
RUN uv pip install --python /app/.venv grpcio-tools==1.66.2 \
 && /app/.venv/bin/python -m grpc_tools.protoc -I proto \
      --python_out=libs/ws_contracts/src --pyi_out=libs/ws_contracts/src \
      --grpc_python_out=libs/ws_contracts/src proto/weaksignals/*/v1/*.proto \
 && find libs/ws_contracts/src/weaksignals -type d -exec sh -c 'test -f "$1/__init__.py" || : > "$1/__init__.py"' _ {} \;

FROM base AS runtime
RUN useradd --create-home --uid 10001 app
COPY --from=build-contracts /app/.venv /app/.venv
COPY --from=build-contracts /app/libs /app/libs
COPY services/orchestrator /app/services/orchestrator
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/orchestrator/src:/app/libs/ws_common/src:/app/libs/ws_contracts/src"
USER app
EXPOSE 8080 8084
ENTRYPOINT ["python", "-m", "orchestrator.main"]
CMD ["api"]
