# analyzer

Сервис выявления кандидатов-слабых сигналов: кластеризует документы коллекции, вычисляет 25
интерпретируемых признаков реестра v1, применяет восемь детерминированных правил исключения,
скорит остальных кандидатов калиброванной моделью, ранжирует и объясняет каждое решение.

gRPC `weaksignals.analyzer.v1.AnalyzerService` на `:50052`, HTTP обслуживания на `:8082`,
схема БД `analyzer`. Стек синхронный (ADR-09): `grpc.server` + `ThreadPoolExecutor(4)`, psycopg 3 sync.

## Запуск

```bash
docker compose up -d --build analyzer          # в составе системы
python -m analyzer.main serve                  # локально
python -m analyzer.main register-model active  # регистрация версии модели без запуска сервера
```

Готовность (`/readyz` 200) наступает только после успешной загрузки активной модели из
`model-store` и модели эмбеддингов e5. При повреждённом артефакте сервис поднимается, но остаётся
не готов, а в лог пишется событие `model.corrupt`.

## RPC

| RPC | Назначение | Дедлайн |
|---|---|---|
| `StartAnalysis` | идемпотентный запуск анализа коллекции | 5 с |
| `GetAnalysis` | статус и статистика | 5 с |
| `ListCandidates` | кандидаты постранично (ранг ↑, затем исключённые по оценке ↓) | 15 с |
| `CancelAnalysis` | кооперативная отмена | 5 с |
| `ScoreText` | прямой скоринг описания технологии | 20 с (90 с с обогащением) |
| `GetModelInfo` | сведения об активной модели | 5 с |

## Конвейер анализа (§12.5 ТЗ)

`StreamDocuments` → эмбеддинги с кешем `document_embeddings` → near-dup (косинус ≥ 0.92) →
фильтр релевантности запросу (≥ 0.25) → агломеративная кластеризация (average linkage, косинус,
порог 0.35) → ключевые фразы (n-граммы 1–3 + MMR, λ = 0.7) → признаки → правила исключения →
модель → ранжирование (score ↓, `trusted_share` ↓, `query_relevance` ↓) → доказательства (≤ 8,
гарантированно с источником HIGH/MEDIUM) → транзакционная запись результата.

Отмена и продление аренды проверяются между шагами: аренда 60 с, heartbeat 20 с.

## Артефакт модели

Каталог версии в `model-store` (обычно `active/`):

```
manifest.json      # model_manifest.schema.json: версия, метрики, порог, sha256 артефактов
classifier.joblib  # модель (logreg_elasticnet или lightgbm)
scaler.joblib      # StandardScaler
calibrator.joblib  # калибровка Платта
centroids.npz      # ключи weak и mature — эталонные центроиды для emb_sim_*
rules.json         # thresholds (пороги правил §12.6) и feature_defaults (заглушки для ScoreText)
```

Все файлы, перечисленные в `artifact_files.sha256`, сверяются по контрольной сумме перед
загрузкой: `joblib` исполняет код при чтении, поэтому непроверенный артефакт не принимается.
`artifact_sha256` в реестре версий — сумма самого `manifest.json`, которая фиксирует состав всех
артефактов сразу.

## Конфигурация

Переменные `WS_*` (полный список — `docs/config_dictionary.md`): `WS_GRPC_PORT=50052`,
`WS_HTTP_PORT=8082`, `WS_COLLECTOR_ADDR`, `WS_MODEL_STORE_DIR=/models`,
`WS_MODEL_REQUIRE_SHA256=true`, `WS_EMBEDDING_MODEL=intfloat/multilingual-e5-base`,
`WS_EMBEDDING_BATCH_SIZE=32`, `WS_TORCH_THREADS=4`, `WS_ANALYZER_WORKERS=1`,
`WS_ANALYZER_MAX_PENDING=20`, `WS_ANALYZER_LEASE_SECONDS=60`, `WS_ANALYZER_MAX_DOCUMENTS=3000`,
`WS_MIN_DOC_QUERY_SIM=0.25`, `WS_NEAR_DUP_THRESHOLD=0.92`, `WS_CLUSTER_DISTANCE_THRESHOLD=0.35`,
`WS_MIN_CLUSTER_SIZE=2`, `WS_EVIDENCE_MAX=8`, `WS_ENRICHMENT_TIMEOUT_SECONDS=60`, `HF_HOME`.

Лексиконы и правила стадий лежат в `config/` и должны совпадать с теми, что использует trainer
при обучении: признаки вычисляются одной и той же функцией `domain/features.py`.

## Тесты

```bash
uv run pytest services/analyzer/tests/unit                       # без сети и БД
uv run pytest services/analyzer/tests/integration -m integration # PostgreSQL через testcontainers
uv run pytest services/analyzer/tests/contract -m contract       # gRPC-сервер в процессе
```

Фактически выполненные проверки и то, что осталось непроверенным, — `docs/analyzer/TEST_REPORT.md`.
