# Словарь конфигурации (переменные окружения `WS_*`)

Версия 1.0.0 (2026-09-15). Все сервисы читают конфигурацию через `ws_common.config` (pydantic-settings). Неверное значение → отказ запуска с сообщением `config.invalid: <VAR>: <причина>`. Обозначения: **С** — секрет (маскируется в логах, только `.env`/окружение), **О** — обязательна для указанного сервиса.

## Общие для всех сервисов

| Переменная | Сервисы | Тип / диапазон | По умолчанию | О/С | Влияние |
|---|---|---|---|---|---|
| WS_SERVICE_NAME | все | `orchestrator-api` \| `orchestrator-worker` \| `collector` \| `analyzer` \| `insight` \| `ui` \| `trainer` | задаётся в compose | О | поле `service` в логах/метриках |
| WS_LOG_LEVEL | все | DEBUG/INFO/WARNING/ERROR | INFO | | уровень логов |
| WS_LOG_FORMAT | все | json \| console | json | | формат stdout |
| WS_HTTP_PORT | все, кроме ui | 1024–65535 | 8080 api; 8081 collector; 8082 analyzer; 8083 insight; 8084 worker | | `/healthz`, `/readyz`, `/metrics` |
| WS_GRPC_PORT | collector, analyzer, insight | 1024–65535 | 50051 / 50052 / 50053 | О | порт gRPC-сервера |
| WS_GRPC_MAX_MESSAGE_MB | все gRPC | 1–64 | 16 | | лимит размера сообщения |
| WS_SHUTDOWN_GRACE_SECONDS | все | 1–120 | 20 | | ожидание завершения RPC при SIGTERM |
| WS_PG_HOST | все с БД | host | postgres | О | |
| WS_PG_PORT | все с БД | 1–65535 | 5432 | | |
| WS_PG_DATABASE | все с БД | имя | weaksignals | О | |
| WS_PG_USER | все с БД | ws_orchestrator / ws_collector / ws_analyzer / ws_insight | по сервису | О | роль = владелец схемы |
| WS_PG_PASSWORD | все с БД | строка | — | О, С | берётся из `WS_<SERVICE>_DB_PASSWORD` в compose |
| WS_PG_POOL_MIN / WS_PG_POOL_MAX | все с БД | 1–50 | 2 / 10 | | пул соединений на процесс |
| WS_PG_STATEMENT_TIMEOUT_MS | все с БД | 1000–600000 | 30000 | | `statement_timeout` сессии |
| WS_MIGRATE_ON_START | все с БД | bool | true | | применение миграций при старте под advisory lock |
| WS_DATA_DIR | trainer, ui | путь | /data | | локальные данные |
| WS_ENV | все | prod \| test | prod | | `test` разрешает провайдер LLM `fake` и фикстурные адаптеры |

## Инициализация PostgreSQL (только контейнер postgres)

| Переменная | Тип | По умолчанию | О/С | Влияние |
|---|---|---|---|---|
| WS_POSTGRES_SUPERUSER_PASSWORD | строка | — | О, С | пароль `postgres`, только init/миграции ролей |
| WS_ORCHESTRATOR_DB_PASSWORD, WS_COLLECTOR_DB_PASSWORD, WS_ANALYZER_DB_PASSWORD, WS_INSIGHT_DB_PASSWORD | строки | — | О, С | пароли ролей-владельцев схем (передаются в `00_roles.sql` psql-переменными) |

## orchestrator (api и worker)

| Переменная | Тип / диапазон | По умолчанию | О/С | Влияние |
|---|---|---|---|---|
| WS_API_BIND | IP | 127.0.0.1 | | адрес публикации API; `0.0.0.0` требует непустой WS_API_KEY |
| WS_API_KEY | строка ≥ 16 символов или пусто | пусто | С | пусто = аутентификация выключена (только локальная демонстрация) |
| WS_IP_HASH_SALT | строка | случайная при старте | С | соль для `client_ip_hash` |
| WS_RATE_LIMIT_POST_PER_MIN | 1–1000 | 10 | | лимит POST /queries на IP |
| WS_QUEUE_MAX_PENDING | 1–10000 | 100 | | 429 QUEUE_FULL |
| WS_COLLECTOR_ADDR, WS_ANALYZER_ADDR, WS_INSIGHT_ADDR | host:port | collector:50051 и т. д. | О | адреса gRPC |
| WS_WORKER_REPLICAS | 1–50 | 1 | | число контейнеров worker (compose) |
| WS_WORKER_CONCURRENCY | 1–20 | 2 | | заданий одновременно в одном worker |
| WS_WORKER_POLL_SECONDS | 0.2–10 | 1.0 | | период опроса очереди |
| WS_JOB_LEASE_SECONDS | 15–600 | 60 | | аренда задания |
| WS_JOB_HEARTBEAT_SECONDS | 5–300 (< lease/2) | 20 | | продление аренды |
| WS_JOB_MAX_ATTEMPTS | 1–10 | 3 | | попытки после истечения аренды |
| WS_STAGE_POLL_SECONDS | 1–30 | 3 | | опрос GetCollection/GetAnalysis |
| WS_COLLECT_TIME_BUDGET_SECONDS | 10–600 | 120 | | бюджет сбора |
| WS_COLLECT_MAX_TOTAL_DOCUMENTS | 1–3000 | 800 | | лимит документов |
| WS_MIN_DOCUMENTS | 1–1000 | 20 | | меньше → FAILED TOO_FEW_DOCUMENTS |
| WS_ANALYZE_TIMEOUT_SECONDS | 30–1800 | 300 | | ожидание анализа |
| WS_INSIGHT_CONCURRENCY | 1–20 | 1 | | параллельных GenerateInsight на задание |
| WS_INSIGHT_TIMEOUT_SECONDS | 10–300 | 90 | | deadline GenerateInsight |
| WS_EVIDENCE_TEXT_MAX_CHARS | 200–2000 | 2000 | | усечение текста доказательств |
| WS_IDEMPOTENCY_TTL_HOURS | 1–168 | 24 | | TTL ключей |
| WS_PROMPT_VERSION | `[a-z]+_v[0-9]+` | insight_v1 | | версия промпта, входящая в ключ идемпотентности GenerateInsight |
| WS_RETENTION_INTERVAL_MINUTES | 5–1440 | 60 | | период задачи очистки |

## collector

| Переменная | Тип / диапазон | По умолчанию | О/С | Влияние |
|---|---|---|---|---|
| WS_OPENALEX_API_KEY | строка | пусто | С | пусто → адаптер openalex `AUTH_MISSING` (отключён) |
| WS_GITHUB_TOKEN | строка | пусто | С | повышает лимит GitHub Search |
| WS_PATENTSVIEW_API_KEY | строка | пусто | С | включает адаптер patentsview |
| WS_COLLECTOR_RSS_FEEDS | список URL через запятую (≥ 1) | 3 ленты из `.env.example` | О | ленты адаптера rss |
| WS_CONTACT_EMAIL | email | team@example.org | | User-Agent для вежливых запросов |
| WS_COLLECTOR_WORKERS | 1–10 | 2 | | одновременных коллекций |
| WS_COLLECTOR_REPLICAS | 1–20 | 1 | | делитель лимитов rps при репликации |
| WS_COLLECTOR_MAX_PENDING | 1–1000 | 50 | | RESOURCE_EXHAUSTED |
| WS_COLLECTOR_LEASE_SECONDS | 15–600 | 60 | | аренда коллекции |
| WS_COLLECTOR_HTTP_TIMEOUT_SECONDS | 3–60 | 15 | | read-таймаут внешних запросов |
| WS_COLLECTOR_HTTP_MAX_BYTES | 1–50 (МБ) | 5 | | лимит размера ответа |
| WS_COLLECTOR_HTTP_RETRIES | 0–5 | 3 | | ретраи 429/5xx |
| WS_ENCYCLOPEDIA_CACHE_DAYS | 1–90 | 7 | | TTL кеша Wikipedia |
| WS_DOCUMENT_RETENTION_DAYS | 7–3650 | 90 | | удаление «осиротевших» документов |
| WS_SOURCE_ENABLED_OVERRIDE | `openalex=true,hh=false,…` | пусто | | переопределение флагов `source_catalog.enabled` без миграции |

## analyzer

| Переменная | Тип / диапазон | По умолчанию | О/С | Влияние |
|---|---|---|---|---|
| WS_COLLECTOR_ADDR | host:port | collector:50051 | О | |
| WS_MODEL_STORE_DIR | путь | /models | О | манифест `active/manifest.json` |
| WS_MODEL_REQUIRE_SHA256 | bool | true | | отказ при несовпадении хеша |
| WS_EMBEDDING_MODEL | HF id | intfloat/multilingual-e5-base | | `-small` при малой RAM; должен совпадать с манифестом модели (иначе отказ) |
| WS_EMBEDDING_BATCH_SIZE | 1–256 | 32 | | |
| WS_TORCH_THREADS | 1–64 | 4 | | `torch.set_num_threads` |
| WS_ANALYZER_WORKERS | 1–8 | 1 | | одновременных анализов |
| WS_ANALYZER_MAX_PENDING | 1–500 | 20 | | RESOURCE_EXHAUSTED |
| WS_ANALYZER_LEASE_SECONDS | 15–600 | 60 | | |
| WS_ANALYZER_MAX_DOCUMENTS | 100–5000 | 3000 | | усечение входа |
| WS_MIN_DOC_QUERY_SIM | 0–1 | 0.25 | | отбрасывание нерелевантных документов |
| WS_NEAR_DUP_THRESHOLD | 0.8–0.99 | 0.92 | | near-dup |
| WS_CLUSTER_DISTANCE_THRESHOLD | 0.1–0.9 | 0.35 | | агломеративная кластеризация |
| WS_MIN_CLUSTER_SIZE | 1–10 | 2 | | |
| WS_EVIDENCE_MAX | 1–8 | 8 | | доказательств на кандидата |
| WS_ENRICHMENT_TIMEOUT_SECONDS | 10–300 | 60 | | ScoreText with_enrichment |
| HF_HOME | путь | /hf-cache | | кеш моделей HF |
| HF_HUB_OFFLINE | 0/1 | из WS_HF_HUB_OFFLINE | | 1 после прогрева — без сети к HF |

## insight

| Переменная | Тип / диапазон | По умолчанию | О/С | Влияние |
|---|---|---|---|---|
| WS_LLM_PRIMARY_PROVIDER | gigachat \| yandexgpt \| local_llamacpp \| fake \| none | gigachat | О | основной провайдер (`fake` — только тесты/CI) |
| WS_LLM_FALLBACK_PROVIDER | те же \| none | yandexgpt | | резерв |
| WS_LLM_LOCAL_ENABLED | bool | false | | третий уровень — локальный llama.cpp |
| WS_GIGACHAT_CREDENTIALS | строка (Authorization key) | — | С | обязателен, если провайдер gigachat включён |
| WS_GIGACHAT_SCOPE | GIGACHAT_API_PERS \| GIGACHAT_API_B2B \| GIGACHAT_API_CORP | GIGACHAT_API_PERS | | тип учётной записи |
| WS_GIGACHAT_MODEL | `GigaChat-2-Pro` \| `GigaChat-2-Max` \| `GigaChat-2` (Lite) | GigaChat-2-Pro | | валидация: только модели семейства GigaChat 2 (N-14) |
| WS_GIGACHAT_LITE_MODEL | как выше | GigaChat-2 | | для ExpandQuery |
| WS_GIGACHAT_MAX_CONCURRENCY | 1–10 | 1 | | физлицо = 1 |
| WS_GIGACHAT_CA_BUNDLE | путь | /etc/ssl/certs/russian_trusted_root_ca.pem | | сертификат НУЦ Минцифры; отключение проверки TLS невозможно |
| WS_YANDEX_API_KEY | строка | — | С | обязателен, если провайдер yandexgpt включён |
| WS_YANDEX_FOLDER_ID | строка | — | | |
| WS_YANDEX_MODEL | `yandexgpt/latest` \| `yandexgpt-lite/latest` \| `yandexgpt/rc` | yandexgpt/latest | | |
| WS_YANDEX_MAX_CONCURRENCY | 1–20 | 4 | | |
| WS_LOCAL_LLM_BASE_URL | URL | пусто | | напр. http://local-llm:8000/v1 |
| WS_LOCAL_LLM_MODEL | строка | local | | имя модели в OpenAI-совместимом API |
| WS_LOCAL_LLM_GGUF, WS_LOCAL_LLM_THREADS | файл; 1–64 | см. .env.example; 6 | | только контейнер local-llm |
| WS_LLM_TIMEOUT_SECONDS | 5–300 | 60 | | на один вызов |
| WS_LLM_MAX_ATTEMPTS | 1–5 | 2 | | повторы при отклонении схемой/grounding |
| WS_LLM_TEMPERATURE | 0–1 | 0.2 | | |
| WS_LLM_DAILY_TOKEN_BUDGET | 0–10^9 (0 = без лимита) | 2000000 | | суточный бюджет токенов на все провайдеры |
| WS_CIRCUIT_BREAKER_FAILURES / WS_CIRCUIT_BREAKER_COOLDOWN_SECONDS | 1–20; 10–3600 | 3 / 60 | | размыкание провайдера |
| WS_INSIGHT_CACHE_DAYS | 0–90 | 7 | | кеш по input_hash |
| WS_INSIGHT_MAX_QUEUE | 1–1000 | 100 | | RESOURCE_EXHAUSTED |
| WS_GROUNDING_MIN_FEATURES | 0–8 | 2 | | признаков в объяснении |
| WS_GROUNDING_MIN_CYRILLIC_SHARE | 0–1 | 0.6 | | проверка языка |

## ui

| Переменная | Тип | По умолчанию | О/С | Влияние |
|---|---|---|---|---|
| WS_API_BASE_URL | URL | http://orchestrator-api:8080 | О | адрес API |
| WS_API_KEY | строка | пусто | С | передаётся в `X-API-Key` |
| WS_UI_BIND | IP | 0.0.0.0 | | адрес Streamlit |
| WS_UI_POLL_SECONDS | 1–30 | 3 | | автообновление результатов |

## trainer (профиль ml)

| Переменная | Тип | По умолчанию | Влияние |
|---|---|---|---|
| WS_COLLECTOR_ADDR | host:port | collector:50051 | обогащение |
| WS_MODEL_STORE_DIR | путь | /models | экспорт артефакта |
| WS_MLFLOW_TRACKING_URI | URI | file:///mlruns | трекинг |
| WS_TRAIN_SEED | int | 20260915 | воспроизводимость |
| WS_TRAIN_DATASET | путь | data/labels/dataset_<latest>.jsonl | вход |
| WS_TRAIN_HOLDOUT_SHARE | 0.1–0.4 | 0.2 | тест |
| WS_TRAIN_CV_REPEATS / WS_TRAIN_CV_FOLDS | 1–10 / 3–10 | 5 / 5 | протокол |
| WS_TRAIN_MIN_PRECISION | 0–1 | 0.80 | ограничение при выборе порога |
