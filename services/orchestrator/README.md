# orchestrator

Единая точка входа системы: принимает запрос пользователя по HTTP, ведёт длительное задание по
стадиям (расширение запроса → сбор → анализ → нарратив), собирает неизменяемый снимок ТОП-N и
отдаёт его интерфейсу вместе со статистикой и причинами исключения кандидатов.

Два процесса из одного пакета: `api` (FastAPI на `:8080`) и `worker` (цикл выполнения заданий,
health/metrics на `:8084`). Схема БД `orchestrator`. Стек асинхронный: FastAPI, psycopg 3 async,
`grpc.aio`.

## Запуск

```bash
docker compose up -d --build orchestrator-api orchestrator-worker
python -m orchestrator.main api      # локально: HTTP API
python -m orchestrator.main worker   # локально: исполнитель заданий
```

`WS_API_BIND` вне `127.0.0.1` без `WS_API_KEY` — отказ запуска: сервис не поднимется открытым в
сеть без аутентификации.

## HTTP API

Нормативный контракт — `docs/api/orchestrator.openapi.yaml`; интерактивная документация на
`/docs`. Аутентификация — заголовок `X-API-Key`.

| Путь | Назначение |
|---|---|
| `POST /api/v1/queries` | приём запроса; обязателен заголовок `Idempotency-Key` |
| `GET /api/v1/jobs` | список заданий (keyset-пагинация, фильтр по статусу) |
| `GET /api/v1/jobs/{job_id}` | состояние и прогресс задания |
| `POST /api/v1/jobs/{job_id}/cancel` | кооперативная отмена |
| `GET /api/v1/jobs/{job_id}/results` | снимок результата: ТОП-N, исключённые, статистика |
| `GET /api/v1/results/items/{item_id}` | элемент выдачи целиком: нарратив, признаки, источники |
| `GET /api/v1/model` | сведения об активной модели (прокси analyzer) |
| `POST /api/v1/score` | прямой скоринг описания (прокси analyzer) |
| `GET /healthz`, `/readyz`, `/metrics` | обслуживание |

Коды ошибок: 400 `VALIDATION_ERROR`, 401 `UNAUTHORIZED`, 404 `NOT_FOUND`,
409 `IDEMPOTENCY_CONFLICT` / `JOB_NOT_CANCELLABLE` / `RESULTS_NOT_READY`,
429 `QUEUE_FULL` / `RATE_LIMITED` (с `Retry-After: 30`), 502 `UPSTREAM_UNAVAILABLE`.

## Жизненный цикл задания

`QUEUED → COLLECTING → ANALYZING → NARRATING → COMPLETED | PARTIAL`, а также `FAILED` и
`CANCELLED` из любой рабочей стадии. Аренда 60 с, heartbeat 20 с; потеря аренды возвращает
задание в очередь (не более трёх попыток, дальше `LEASE_EXPIRED_MAX_ATTEMPTS`).

`COMPLETED` выставляется только при полном результате: элементов ровно `top_n`, все нарративы
сгенерированы, коллекция завершилась `COMPLETED` и ни один адаптер не отказал. Иначе `PARTIAL`
с разбором причин в `error_message`: `PARTIAL:found=9<15;fallback_narratives=2;adapters_failed=rss`.

Идентификаторы коллекции и анализа сохраняются сразу после их создания: повторная попытка
переиспользует их, а отмена доходит до collector и analyzer даже в середине опроса.

## Деградация

| Отказ | Поведение |
|---|---|
| insight недоступен или выключен (`WS_INSIGHT_ENABLED=false`) | стадия расширения даёт резервные термины (`expand_used_fallback=true`), нарративы собираются экстрактивно из доказательств и помечаются `FALLBACK_EXTRACTIVE`; задание завершается `PARTIAL` |
| collector вернул мало документов | `FAILED` с кодом `TOO_FEW_DOCUMENTS` без повторов |
| collector или analyzer недоступны | задание откладывается в очередь на 30 с без увеличения попытки (до 10 раз) |
| documents недоступны для кандидата | элемент собирается без источников, а не теряется целиком |

## Конфигурация

`WS_HTTP_PORT=8080`, `WS_API_BIND`, `WS_API_KEY`, `WS_IP_HASH_SALT`,
`WS_RATE_LIMIT_POST_PER_MIN=30`, `WS_QUEUE_MAX_PENDING=3`, `WS_COLLECTOR_ADDR`,
`WS_ANALYZER_ADDR`, `WS_INSIGHT_ADDR`, `WS_INSIGHT_ENABLED`, `WS_WORKER_CONCURRENCY=1`,
`WS_WORKER_POLL_SECONDS=2`, `WS_JOB_LEASE_SECONDS=60`, `WS_JOB_HEARTBEAT_SECONDS=20`,
`WS_STAGE_POLL_SECONDS=3`, `WS_COLLECT_TIME_BUDGET_SECONDS=120`,
`WS_COLLECT_MAX_TOTAL_DOCUMENTS=800`, `WS_MIN_DOCUMENTS=20`, `WS_ANALYZE_TIMEOUT_SECONDS=300`,
`WS_EVIDENCE_TEXT_MAX_CHARS=2000`, `WS_PROMPT_VERSION=insight_v1`,
`WS_IDEMPOTENCY_TTL_HOURS=24`, `WS_RETENTION_INTERVAL_MINUTES=60`.

## Тесты

```bash
uv run pytest services/orchestrator/tests/unit
uv run pytest services/orchestrator/tests/contract -m contract
uv run pytest services/orchestrator/tests/integration -m integration   # нужен Docker
```

Фактически выполненные проверки — `docs/orchestrator/TEST_REPORT.md`.
