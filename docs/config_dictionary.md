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
| WS_COLLECT_TIME_BUDGET_SECONDS | 10–600 | 480 | | бюджет сбора: широкий и прицельный проходы (этап 4.7; было 240 — у всех 13 источников DEADLINE_REACHED); быстрый замер берёт его же |
| WS_COLLECT_MAX_TOTAL_DOCUMENTS | 1–3000 | 3000 | | лимит документов сбора (этап 5.0.1: было 1450 — лимит заканчивал сбор за ~110 с) |
| WS_MIN_DOCUMENTS | 1–1000 | 20 | | меньше → FAILED TOO_FEW_DOCUMENTS |
| WS_ANALYZE_TIMEOUT_SECONDS | 0–14400 | 0 | | ожидание анализа; 0 — без отдельного лимита, только срок задания (этап 5.0.3); иначе max(этого, документов × WS_ANALYZE_SECONDS_PER_DOCUMENT) |
| WS_ANALYZE_SECONDS_PER_DOCUMENT | 0–10 | 0.5 | | секунд ожидания анализа на документ коллекции (этап 5.0) |
| WS_ANALYZER_MAX_CANDIDATES | 5–500 | 150 | | кандидатов analyzer для модели в режиме legacy (этап 5.0; было 40) |
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
| WS_GITHUB_TOKEN | строка | пусто | С | GitHub Search: 30 запросов/мин с токеном; без него сбор ограничивает поиск 1 запросом в 7 с |
| WS_PATENTSVIEW_API_KEY | строка | пусто | С | включает адаптер patentsview |
| WS_COLLECTOR_RSS_FEEDS | список URL через запятую (≥ 1) | 3 ленты из `.env.example` | О | ленты адаптера rss |
| WS_CONTACT_EMAIL | email | team@example.org | | User-Agent для вежливых запросов |
| WS_COLLECTOR_WORKERS | 1–10 | 2 | | одновременных коллекций |
| WS_COLLECTOR_REPLICAS | 1–20 | 1 | | делитель лимитов rps при репликации |
| WS_COLLECTOR_MAX_PENDING | 1–1000 | 50 | | RESOURCE_EXHAUSTED |
| WS_COLLECTOR_LEASE_SECONDS | 15–600 | 60 | | аренда коллекции |
| WS_COLLECTOR_HTTP_TIMEOUT_SECONDS | 3–60 | 15 | | read-таймаут внешних запросов |
| WS_COLLECTOR_HTTP_MAX_BYTES | 1–50 (МБ) | 5 | | лимит размера ответа |
| WS_COLLECTOR_HTTP_RETRIES | 0–5 | 3 | | с этапа 4.2 HTTP-клиент источников не повторяет запросы: повторы делает адаптер через лимитер (политика источника, `transient`) |
| WS_ENCYCLOPEDIA_CACHE_DAYS | 1–90 | 7 | | TTL кеша Wikipedia |
| WS_DOCUMENT_RETENTION_DAYS | 7–3650 | 90 | | удаление «осиротевших» документов |
| WS_SOURCE_ENABLED_OVERRIDE | `openalex=true,hh=false,…` | gdelt=false (этап 5.1) | | переопределение флагов `source_catalog.enabled` без миграции |
| WS_COLLECTOR_TRACE_ENABLED | bool | true | | трасса сбора для режима А: сырые ответы источников без ключей и события документов (`raw_responses`, `document_events`) |
| WS_GDELT_HISTORY_MONTHS | 3–120 | 24 | | глубина истории новостей GDELT: окна по 90 дней, не раньше `published_since_year` |
| WS_GDELT_CONNECT_TIMEOUT_SECONDS | 3–60 | 20 | | таймаут соединения с GDELT (остальные источники — 5 с) |
| WS_SEMANTIC_SCHOLAR_WINDOW_MONTHS | 3–120 | 24 | | окно публикаций bulk-поиска Semantic Scholar |
| WS_HACKERNEWS_HISTORY_MONTHS | 3–120 | 24 | | глубина истории публикаций Hacker News (Search by Algolia): окна по 90 дней |
| WS_COLLECTOR_TARGETED_ENABLED | true/false | true | | прицельный проход по технологиям после широкого (этап 4) |
| WS_COLLECTOR_TARGETED_PER_TERM | 1–10 | 3 | | документов на технологию в каждом источнике прицельного прохода |
| WS_COLLECTOR_BROAD_TIME_SHARE | 0,2–0,9 | 0,5 | | доля бюджета времени сбора на широкий проход |
| WS_COLLECTOR_TARGETED_RESERVE_DOCUMENTS | 0–2000 | 600 | | места общего лимита, которые широкий проход оставляет прицельному (не больше половины лимита; этап 4.4: было 300) |
| WS_COLLECTOR_SNOWBALL_RESERVE_DOCUMENTS | 0–1000 | 200 | | места второго круга: прицельный проход оставляет их свободными (не больше четверти лимита) |
| WS_COLLECTOR_CROSSREF_BROAD_DOCUMENTS | 0–500 | 20 | | лимит широкого прохода Crossref |
| WS_COLLECTOR_HUGGINGFACE_BROAD_DOCUMENTS | 0–500 | 10 | | лимит широкого прохода Hugging Face |
| WS_COLLECTOR_NEWS_ARCHIVE_BROAD_DOCUMENTS | 0–500 | 20 | | лимит широкого прохода архива новостей |
| WS_COLLECTOR_NEWS_ARCHIVE_PER_TERM | 1–20 | 5 | | статей архива новостей на технологию в прицельном проходе |
| WS_COLLECTOR_EMERGENCE_SAMPLE | 1000–50000 | 15000 | | работ корпуса arXiv на период выборки оценки возникновения |
| WS_COLLECTOR_NEWS_EMERGENCE_SAMPLE | 1000–50000 | 10000 | | статей архива на период выборки оценки возникновения |
| WS_COLLECTOR_EVENT_CHANNEL_DOCUMENTS | 0–2000 | 400 | | статей канала «события стартапов» в начале прицельного прохода архива (0 — канал выключен; этап 4.7: было 150) |
| WS_COLLECTOR_EVENT_MIN_OVERLAP | 1–5 | 2 | | сколько слов словаря (слова темы и отобранных технологий) должно быть в статье канала |
| WS_COLLECTOR_EVENT_POOL | 100–100000 | 30000 | | страховочный предел кандидатов канала событий; выше наибольшего числа кандидатов темы, чтобы ранжирование по редкости шло по всем (этап 4.7.1) |
| WS_COLLECTOR_EMERGING_QUOTA | 0–40 | 15 | | квота технологий из корпусов при выборе по очереди из каналов |
| WS_COLLECTOR_MINED_QUOTA | 0–40 | 9 | | квота извлечённых технологий |
| WS_COLLECTOR_ENTITY_QUOTA | 0–40 | 6 | | квота компаний и продуктов из заголовков событий |
| WS_COLLECTOR_EMERGENCE_MIN_GROWTH | 1–10 | 1.5 | | минимальный рост доли термина (стабильные и падающие — не возникновение) |
| WS_COLLECTOR_EMERGENCE_MIN_BASE | 0–100000 | 300 | | минимум документов базового периода; меньше — рост неизвестен |
| WS_ARCHIVE_ENRICH_PAUSE_SECONDS | 0.5–60 | 2 | | пауза между загрузками архивных копий страниц (`archive enrich`) |
| WS_ARCHIVE_DISCOVER_PAUSE_SECONDS | 0.2–60 | 1 | | пауза между запросами поиска лент (`archive discover-feeds`) |
| WS_EXPAND_CACHE_TTL_HOURS | 0–8760 | 168 | | срок жизни кеша расширения запроса в insight; 0 — без срока |
| WS_EXPAND_CACHE_MODEL | строка | пусто | | брать из кеша только ответы этой модели (пусто — любой) |
| WS_COLLECTOR_CROSSREF_PER_TERM | 1–10 | 1 | | документов Crossref на технологию в прицельном проходе |
| WS_COLLECTOR_NEWS_EMERGENCE_ORIGIN | rss, wayback или пусто | rss | | происхождение заголовков для оценки возникновения по новостям |
| WS_COLLECTOR_NEWS_EMERGENCE_MIN_ROWS | 0–100000 | 2000 | | минимум заголовков недавнего периода для оценки возникновения по новостям |
| WS_COLLECTOR_BROAD_MAX_DOCUMENTS_PER_SOURCE | 10–500 | 60 | | лимит широкого прохода на источник при двух проходах; RSS не ограничивается |
| WS_COLLECTOR_MINED_TECHNOLOGIES | 0–40 | 15 | | сколько технологий извлекать из заголовков широкого прохода |
| WS_COLLECTOR_TARGETED_WINDOW_MONTHS | 3–120 | 24 | | окно дат прицельного поиска |
| WS_SINGLETON_CANDIDATES | 0–20 | 0 | | одиночные документы, ближайшие к запросу, остаются кандидатами (не больше четверти пула) |
| WS_ARCHIVE_WAYBACK_FROM | ГГГГ-ММ | 2025-01 | | первый месяц первичной загрузки архива новостей из Wayback Machine |
| WS_ARCHIVE_WAYBACK_PAUSE_SECONDS | 1–60 | 3 | | пауза между запросами к Wayback CDX API |
| WS_ARCHIVE_REFRESH_MONTHS | 0–12 | 2 | | сколько последних месяцев перечислять заново при ежемесячном запуске |
| WS_NEWS_ARCHIVE_WINDOW_MONTHS | 3–120 | 24 | | окно дат поиска по архиву новостей |
| WS_CORPUS_ARXIV_SETS | список | cs,eess,q-fin | | наборы arXiv для корпуса (OAI-PMH) |
| WS_CORPUS_SINCE_YEAR | 2015–2030 | 2021 | | работы корпуса не старше этого года (база оценки возникновения — до последних 24 месяцев) |
| WS_CORPUS_OAI_PAUSE_SECONDS | 1–60 | 3 | | пауза между страницами OAI-PMH |
| WS_COLLECTOR_EMERGING_TECHNOLOGIES | 0–40 | 25 | | технологий из корпусов по оценке возникновения |
| WS_COLLECTOR_LLM_TECHNOLOGIES | 0–40 | 10 | | технологий от модели — вспомогательно, после корпусов и извлечённых |
| WS_COLLECTOR_SNOWBALL_TECHNOLOGIES | 0–40 | 15 | | названий второго круга из документов прицельного прохода |
| WS_COLLECTOR_TARGETED_ROUNDS | 1–2 | 2 | | 2 — второй круг прицельного поиска включён |
| WS_COLLECTOR_SOURCE_POLICY | `источник:requests=N,retries=N,pause=С,transient=N;…` | пусто | | бюджет запросов на сбор, повторы после 429, пауза и повторы после сетевого сбоя/5xx по источникам поверх значений по умолчанию: Semantic Scholar 50/2/10, Hacker News 60/1/10, GDELT 8 запросов; `transient` по умолчанию 1, у arXiv, Zenodo и GDELT 0 (у них свой шлюз) |

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
| WS_MIN_DOC_QUERY_SIM | 0–1 | 0.0 | | абсолютный порог релевантности (шкала e5 сжата, по умолчанию выключен) |
| WS_RELEVANCE_OUTLIER_MAD | 0–10 | 3.0 | | отсев только явных выбросов: релевантность ниже медианы на N MAD (этап 5.0) |
| WS_VECTOR_SPACE | centered/raw | centered | | пространство для near-dup, отбора и кластеризации (этап 5.0) |
| WS_NEAR_DUP_THRESHOLD | 0.5–1.0 | 0.90 | | near-dup в центрированном пространстве (этап 5.0) |
| WS_CLUSTER_DISTANCE_THRESHOLD | 0.05–1.5 | 0.60 | | порог в центрированном пространстве; в raw прежний смысл (около 0.10) |
| WS_MIN_CLUSTER_SIZE | 1–50 | 1 | | одиночный документ — тоже кандидат (этап 5.0) |
| WS_EVIDENCE_MAX | 1–8 | 8 | | доказательств на кандидата |
| WS_ENRICHMENT_TIMEOUT_SECONDS | 10–300 | 60 | | ScoreText with_enrichment |
| HF_HOME | путь | /hf-cache | | кеш моделей HF |
| HF_HUB_OFFLINE | 0/1 | из WS_HF_HUB_OFFLINE | | 1 после прогрева — без сети к HF |
| WS_ANALYZER_CAPTURE_ENABLED | bool | true | | запись входов анализа для режима А: векторы, ответы Wikipedia, дата признаков, воронка (`analysis_captures`) |

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
| WS_LLM_RECORD_ENABLED | bool | true | | полные тексты промптов и ответов LLM для режима А (`llm_recordings`); решение владельца 03.10.2026 вместо §14.5 ТЗ |

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

## Этап 5.0: расписание архивов и события темы

| Переменная | Диапазон | По умолчанию | Секрет | Назначение |
| --- | --- | --- | --- | --- |
| WS_ARCHIVE_SCHEDULE_ENABLED | true/false | true | | фоновые загрузки архивов внутри collector |
| WS_ARCHIVE_SCHEDULE_RSS_HOURS | 0–8760 | 24 | | интервал чтения лент (0 — выключено) |
| WS_ARCHIVE_SCHEDULE_ARXIV_HOURS | 0–8760 | 24 | | инкремент корпуса arXiv |
| WS_ARCHIVE_SCHEDULE_ENRICH_HOURS | 0–8760 | 6 | | текст статей: сначала из сборов, затем общий архив |
| WS_ARCHIVE_SCHEDULE_SITEMAPS_HOURS | 0–8760 | 24 | | свежие статьи из карт сайтов ресурсов реестра |
| WS_ARCHIVE_SCHEDULE_WAYBACK_HOURS | 0–8760 | 168 | | Wayback: новые и последние месяцы |
| WS_ARCHIVE_SCHEDULE_DISCOVER_HOURS | 0–8760 | 720 | | поиск лент ресурсов реестра |
| WS_ARCHIVE_SCHEDULE_ENRICH_BATCH | 1–20000 | 500 | | записей за один запуск обогащения |
| WS_COLLECTOR_ENTITY_EVENT_DOCUMENTS | 0–2000 | 200 | | статьи о событиях по словам темы без словаря технологий |

## Этап 5.1: отбор кандидатов

| Переменная | Диапазон | По умолчанию | Секрет | Назначение |
| --- | --- | --- | --- | --- |
| WS_CLUSTER_THRESHOLD_MODE | adaptive/fixed | adaptive | | порог кластеризации: по расстояниям до ближайшего соседа в коллекции или фиксированный WS_CLUSTER_DISTANCE_THRESHOLD |
| WS_CLUSTER_NN_QUANTILE | 0.1–0.95 | 0.6 | | квантиль расстояний до ближайшего соседа; одиночных кластеров — около (1 − квантиль) |
| WS_CLUSTER_THRESHOLD_MIN | 0.05–1.5 | 0.30 | | нижняя граница адаптивного порога |
| WS_CLUSTER_THRESHOLD_MAX | 0.05–1.5 | 0.85 | | верхняя граница адаптивного порога |
| WS_PRESELECT_BY_MODEL | true/false | true | | предварительная оценка моделью всех кластеров; кандидаты — лучшие по ней |
| WS_CANDIDATE_RELEVANCE_OUTLIER_MAD | 0–10 | 3.0 | | «слабо связан с запросом» — только близость ниже медианы на N MAD; 0 — абсолютный WS_MIN_CANDIDATE_QUERY_SIM |
| WS_MIN_CANDIDATE_QUERY_SIM | 0–1 | 0 | | абсолютный порог близости кандидата к запросу; при относительном правиле не используется |

## Патч proven_fixes (17.10.2026)

| Переменная | Диапазон | По умолчанию | Секрет | Назначение |
| --- | --- | --- | --- | --- |
| WS_RUBRIC_POOL_RESEARCH_SINGLES | true/false | true | | U-02: одиночные научные работы, препринты и патенты — в пул рубрики |
| WS_PRESELECT_MODEL_SHARE | 0–1 | 0.5 | | U-04: доля мест предотбора по оценке модели; остальные — по близости к запросу |
| WS_COLLECTOR_TECH_FILTER_WORD_LEVEL | true/false | true | | R-043: фразы технологий сравниваются по словам (блокировка и поглощение) |
| WS_RUBRIC_PROMPT_VERSION | judge_v6/judge_v7/judge_v8 | judge_v6 | | версия промпта рубрики; judge_v7 — критерий зрелости (не прошёл замер); judge_v8 — зрелость по области с общеизвестными знаниями о её возрасте (этап 5.3) |
| WS_RANK_MODEL_FIRST_SHARE | 0–1 | 0.5 | | этап 5.3: доля кандидатов с лучшей оценкой модели analyzer, которые ставятся в выдаче первыми; 0 — прежний порядок |
| WS_MATURITY_CHECK_ENABLED | true/false | false | | этап 5.4: второй проход зрелости после рубрики для кандидатов с научными источниками |
| WS_MATURITY_CHECK_MAX_YEAR | 1950–2026 | 2015 | | задача «давняя», если её решают с этого года или раньше (значение — из maturity_check_eval.py) |
| WS_MATURITY_CHECK_NOVELTY | список | improvement,survey | | виды новизны, при которых давняя широкая задача считается зрелой |
| WS_MATURITY_FILTER_ENABLED | true/false | false | | этап 5.5: orchestrator отсеивает готовые карточки с научными источниками, которые локальная модель зрелости считает зрелыми |
| WS_MATURITY_MODEL_FILE | имя файла | maturity_bgem3_v1.json | | этап 5.5: файл локальной модели зрелости в хранилище моделей analyzer |

## Этап 5.4 — зрелые технологии в выдаче (19.10.2026)

Действует в режиме `WS_SELECTION_MODE=rubric` при `WS_FINAL_SPECIFICITY_CHECK=true`.

| Переменная | Диапазон | По умолчанию | Секрет | Назначение |
| --- | --- | --- | --- | --- |
| WS_RUBRIC_FINAL_PROMPT_VERSION | judge_v6 … judge_v11 | judge_v6 | | insight: версия промпта финальной проверки готовой карточки; judge_v11 — N-MAT только при массовом практическом применении пары «механизм — задача» с названным признаком; judge_v9, judge_v10 — варианты замера; judge_v6 — прежнее поведение; judge_v11 включать после замера на GigaChat |
| WS_FINAL_MATURE_RESEARCH_ONLY | true/false | true | | orchestrator: N-MAT финальной проверки убирает только карточки без рыночных источников; рыночные остаются, причина — в трассе `final_mature_kept` |
| WS_FINAL_MATURE_MIN_CONFIDENCE | 0–1 | 0 | | orchestrator: N-MAT убирает карточку только при уверенности судьи не ниже порога; 0 — не применяется (у GigaChat уверенность насыщена) |
| WS_DISPLAY_ORDER | confidence/model_first | confidence | | порядок карточек в выдаче: по индексу уверенности или прежний «модель analyzer первой» |
