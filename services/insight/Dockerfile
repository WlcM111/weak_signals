# Образ insight: gRPC-сервер нарративов. Контекст сборки — корень репозитория.
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
uv sync --no-dev --package insight --no-install-workspace $([ -f uv.lock ] && echo --frozen)

FROM deps AS build-contracts
COPY proto proto
COPY libs libs
RUN uv pip install --python /app/.venv grpcio-tools==1.66.2 \
 && /app/.venv/bin/python -m grpc_tools.protoc -I proto \
      --python_out=libs/ws_contracts/src --pyi_out=libs/ws_contracts/src \
      --grpc_python_out=libs/ws_contracts/src proto/weaksignals/*/v1/*.proto \
 && find libs/ws_contracts/src/weaksignals -type d -exec sh -c 'test -f "$1/__init__.py" || : > "$1/__init__.py"' _ {} \;

FROM base AS runtime
# Сертификат НУЦ Минцифры нужен для проверки TLS при обращении к GigaChat (§17 HANDOFF).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
&& curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors https://gu-st.ru/content/Other/doc/russian_trusted_root_ca.cer \
      -o /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
 && update-ca-certificates \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 app
COPY --from=build-contracts /app/.venv /app/.venv
COPY --from=build-contracts /app/libs /app/libs
COPY services/insight /app/services/insight
# Нормативный валидатор JSON-схем: json_output.py ищет его в каталоге tools рядом с корнем.
COPY tools/minischema.py /app/tools/minischema.py
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/services/insight/src:/app/libs/ws_common/src:/app/libs/ws_contracts/src:/app/tools" \
    WS_GIGACHAT_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
USER app
EXPOSE 50053 8083
ENTRYPOINT ["python", "-m", "insight.main"]
CMD ["serve"]
