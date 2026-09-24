# collector — сервис сбора документов из открытых источников

Контракты 1.0.0 (`proto/weaksignals/{common,collector}/v1`). gRPC-сервер `CollectorService` на `:50051`,
HTTP обслуживания на `:8081`, схема PostgreSQL `collector`.

## Что делает

- `StartCollection` — идемпотентный запуск сбора (SEARCH / ENRICHMENT) по фразам ru/en;
- внутренний воркер `RunCollection` — параллельный обход адаптеров с общим бюджетом времени,
  round-robin интерливинг рангов, дедупликация (URL → DOI → content hash), запись партиями ≤ 50
  документов в одной транзакции, аренда с heartbeat и кооперативная отмена;
- `GetCollection`, `StreamDocuments` (keyset, возобновление по `page_token`), `GetDocuments`,
  `CancelCollection`, `CheckEncyclopedia` (индикатор зрелости по Wikipedia с кешем);
- ретенция документов и кеша (раз в час).

Адаптеры v1: `openalex`, `arxiv`, `rss`, `github` (сбор); `wikipedia` (только `CheckEncyclopedia`);
`patentsview`, `hh` — каркасы, отключены в каталоге до подтверждения условий API.

## Запуск

```bash
cp .env.example .env                  # заполнить WS_COLLECTOR_DB_PASSWORD, WS_OPENALEX_API_KEY, ленты RSS
uv lock                               # зависимости изменились: пересобрать lock-файл
bash tools/gen_proto.sh               # сгенерировать стабы в libs/ws_contracts/src
docker compose up -d --build postgres collector
docker compose logs -f collector
```

Без Docker (локальная отладка):

```bash
uv sync --package collector
bash tools/gen_proto.sh
export PYTHONPATH=services/collector/src:libs/ws_common/src:libs/ws_contracts/src
export WS_PG_HOST=127.0.0.1 WS_PG_USER=ws_collector WS_PG_PASSWORD=... WS_OPENALEX_API_KEY=...
export WS_COLLECTOR_RSS_FEEDS=https://www.cnews.ru/inc/rss.xml,https://arstechnica.com/feed/
python -m collector.main serve
```

Миграции применяются автоматически при старте (`ws_common.migrate`, `pg_advisory_lock`).

## Проверка работоспособности

```bash
# здоровье и метрики
curl -fs http://127.0.0.1:8081/healthz
curl -fs http://127.0.0.1:8081/readyz
curl -fs http://127.0.0.1:8081/metrics | head
python -m ws_common.healthcheck grpc 127.0.0.1:50051

# сбор по открытому запросу (grpcurl; порт 50051 публикуется через compose.override.example.yaml)
grpcurl -plaintext -import-path proto -proto proto/weaksignals/collector/v1/collector.proto \
  -d '{"idempotency_key":"demo-0001:collect","query_text":"технологии защиты ИИ-систем",
       "terms":{"ru":["защита ИИ-систем"],"en":["ai security","llm guardrails"]},"mode":"COLLECTION_MODE_SEARCH"}' \
  127.0.0.1:50051 weaksignals.collector.v1.CollectorService/StartCollection

grpcurl -plaintext -import-path proto -proto proto/weaksignals/collector/v1/collector.proto \
  -d '{"collection_id":"<из ответа>"}' \
  127.0.0.1:50051 weaksignals.collector.v1.CollectorService/GetCollection
```

Ожидаемый результат: статус проходит PENDING → RUNNING → COMPLETED/PARTIAL; `documents_total > 0`,
`adapter_runs` содержит по строке на адаптер с числом запросов и найденных документов.

## Тесты

```bash
uv run pytest services/collector/tests/unit                      # без сети и БД
uv run pytest services/collector/tests/contract -m contract      # настоящий gRPC-транспорт
uv run pytest services/collector/tests/integration -m integration # PostgreSQL 18 через testcontainers (нужен Docker)
python tools/check_proto_conformance.py                          # код ↔ .proto
python tools/check_sql_conformance.py                            # SQL ↔ DDL миграции
```

Модульные тесты запускаются и без pytest: `python -m unittest discover -s services/collector/tests -t .`
(нужен `PYTHONPATH=services/collector/src:libs/ws_common/src`).

## Конфигурация

Обязательные: `WS_PG_HOST`, `WS_PG_DATABASE`, `WS_PG_USER`, `WS_PG_PASSWORD`, `WS_GRPC_PORT`,
`WS_COLLECTOR_RSS_FEEDS` (для адаптера rss), `WS_OPENALEX_API_KEY` (иначе openalex отключается с
кодом `AUTH_MISSING`).

Необязательные: `WS_GITHUB_TOKEN`, `WS_PATENTSVIEW_API_KEY`, `WS_CONTACT_EMAIL`, `WS_COLLECTOR_WORKERS`,
`WS_COLLECTOR_REPLICAS`, `WS_COLLECTOR_MAX_PENDING`, `WS_COLLECTOR_LEASE_SECONDS`,
`WS_COLLECTOR_HEARTBEAT_SECONDS`, `WS_COLLECTOR_HTTP_TIMEOUT_SECONDS`, `WS_COLLECTOR_HTTP_MAX_BYTES`,
`WS_COLLECTOR_HTTP_RETRIES`, `WS_ENCYCLOPEDIA_CACHE_DAYS`, `WS_DOCUMENT_RETENTION_DAYS`,
`WS_SOURCE_ENABLED_OVERRIDE`, `WS_LOG_LEVEL`, `WS_LOG_FORMAT`, `WS_HTTP_PORT`, `WS_ENV`.

Правила доверенности и категории доменов лент — `config/trust_rules.yaml`, `config/rss_domains.yaml`
(читаются при старте, пересборка образа не нужна).

## Границы

Сервис не считает эмбеддинги, не кластеризует и не скорит документы (это analyzer), не скачивает
полные тексты и PDF, не парсит произвольные сайты: обращения идут только к хостам из белого списка
адаптера и к лентам из конфигурации.
