# Состав комплекта `weak-signals-1.2.0` (полная система: пять сервисов + ML-конвейер)

Комплект — набор файлов с путями относительно корня репозитория `weak-signals` (созданного
`scaffold/create_project_structure.sh`). Реализованы **все шесть компонентов**: `collector`, `analyzer`, `ml` (trainer), `orchestrator`,
`insight` и `ui`. В поставку добавлена корневая инфраструктура запуска: `compose.yaml`,
`.env.example`, `db/init`, `pyproject.toml`, `Makefile`. Ранее поставленные компоненты не
менялись, кроме прогнанной регрессии. Существующие файлы проекта
не изменяются: `compose.yaml` уже содержит оба сервиса в том виде, в каком они реализованы
(`entrypoint: python -m <service>.main serve`, healthcheck через `ws_common.healthcheck`,
порты 50051/8081 и 50052/8082), `.env.example` уже содержит нужные переменные.

## Нормативные копии (побайтово совпадают с приложениями комплекта проектирования)

| Путь | sha256 (префикс) |
|---|---|
| `proto/weaksignals/common/v1/common.proto` | `7bc146362ace163a…` |
| `proto/weaksignals/collector/v1/collector.proto` | `c7ace3bd60e4b791…` |
| `proto/weaksignals/analyzer/v1/analyzer.proto` | `a83095902470b9af…` |
| `services/collector/migrations/0001_init.sql` | `c9a3ba0acddf754e…` |
| `services/analyzer/migrations/0001_init.sql` | `195d43b646621a14…` |
| `schemas/feature_registry_v1.json` | `7c8c93b61ae3e99f…` |
| `schemas/model_manifest.schema.json` | `c2961ad070eb9291…` |
| `proto/weaksignals/insight/v1/insight.proto` | `a25e6145b20ad934…` |
| `services/orchestrator/migrations/0001_init.sql` | `b7a41d67b271bdbc…` |
| `docs/api/orchestrator.openapi.yaml` | `1ef12e3c15753827…` |
| `services/insight/migrations/0001_init.sql` | `9587d19d59238841…` |
| `services/insight/schemas/insight_llm_output.schema.json` | `2655640ea221ed90…` |
| `services/insight/schemas/expand_llm_output.schema.json` | `8c565d18648ea680…` |
| `schemas/training_row.schema.json` | `bfb5cf8efbaa383b…` |
| `ml/data/raw/dataset_profile.json` | `60fc0b2a5aaf4987…` |
| `ml/data/raw/dataset_normalized.csv` | нормализованный датасет организаторов (100 строк) |

## Общая библиотека `libs/ws_common`

`pyproject.toml`, `src/ws_common/{__init__,config,logging,clock,ids,errors,db,migrate,metrics,grpc_interceptors,grpc_clients,healthcheck}.py`

Дополнено для analyzer (асинхронные функции collector-а не менялись): `db.build_sync_pool`,
`migrate.apply_migrations_sync`, `clock.SyncClock`/`SyncSystemClock`,
`grpc_interceptors.SyncObservabilityInterceptor`, новый модуль `grpc_clients.py`, метрики анализа
в `metrics.py` (`ws_embeddings_computed_total`, `ws_embedding_cache_hits_total`,
`ws_analysis_duration_seconds`, `ws_candidates_total`, `ws_analyses_total`, `ws_model_info`).

## Контракты `libs/ws_contracts`

`pyproject.toml` (стабы генерируются `tools/gen_proto.sh` в `libs/ws_contracts/src`).

## Сервис `services/collector`

| Каталог | Файлы |
|---|---|
| корень | `Dockerfile`, `pyproject.toml`, `README.md` |
| `config/` | `trust_rules.yaml`, `rss_domains.yaml` |
| `src/collector/` | `main.py`, `config.py`, `config_parsing.py` |
| `src/collector/domain/` | `entities.py`, `values.py`, `rules.py`, `classify.py`, `dedup.py`, `normalization.py`, `errors.py` |
| `src/collector/application/` | `ports.py`, `dto.py`, `validation.py`, `use_cases/` (8 сценариев) |
| `src/collector/adapters/inbound/` | `grpc_server.py`, `mappers.py`, `http_ops.py`, `worker.py` |
| `src/collector/adapters/outbound/` | `http_client.py`, `rate_limiter.py`, `rules_loader.py`, `metrics.py`, `sources/` (7 адаптеров + база + `xml_utils.py`) |
| `src/collector/adapters/outbound/postgres/` | `mappers.py`, `collection_repository.py`, `document_repository.py`, `adapter_run_repository.py`, `encyclopedia_cache.py`, `source_catalog.py` |
| `tests/` | `conftest.py`, `fakes.py`, `unit/` (142 теста), `contract/`, `integration/`, `fixtures/http/` |

## Сервис `services/analyzer`

| Каталог | Файлы |
|---|---|
| корень | `Dockerfile`, `pyproject.toml`, `README.md` |
| `config/` | `stage_rules.yaml`, `lexicons/` (9 файлов: emergence/maturity/hype ru+en, bigtech, stopwords ru+en) |
| `migrations/` | `0001_init.sql` (нормативная копия) |
| `src/analyzer/` | `main.py` (`serve`, `register-model`), `config.py` |
| `src/analyzer/domain/` | `values.py`, `entities.py`, `errors.py`, `feature_registry.py`, `features.py`, `rules.py`, `clustering.py`, `keyphrases.py`, `evidence.py` |
| `src/analyzer/application/` | `ports.py`, `dto.py`, `validation.py`, `scoring.py`, `active_model.py` |
| `src/analyzer/application/use_cases/` | `activate_model.py`, `start_analysis.py`, `run_analysis.py`, `get_analysis.py`, `list_candidates.py`, `cancel_analysis.py`, `score_text.py`, `get_model_info.py` |
| `src/analyzer/adapters/inbound/` | `grpc_server.py`, `mappers.py`, `http_ops.py`, `worker.py` |
| `src/analyzer/adapters/outbound/` | `model_store.py`, `sklearn_model.py`, `e5_embedder.py`, `collector_grpc.py`, `config_loader.py`, `metrics.py` |
| `src/analyzer/adapters/outbound/postgres/` | `mappers.py`, `analysis_repository.py`, `candidate_repository.py`, `embedding_cache.py`, `model_version_repository.py` |
| `tests/` | `conftest.py`, `fakes.py`, `model_fixture.py`, `unit/` (191 тест), `integration/` (9 тестов на testcontainers), `contract/` (9 тестов gRPC) |

## Сервис `services/orchestrator`

| Каталог | Файлы |
|---|---|
| корень | `Dockerfile`, `pyproject.toml`, `README.md` |
| `config/` | `glossary_ru_en.yaml` — резервный словарь ru→en для стадии расширения |
| `migrations/` | `0001_init.sql` (нормативная копия, 10 таблиц) |
| `src/orchestrator/` | `main.py` (`api`, `worker`), `config.py` |
| `src/orchestrator/domain/` | `entities.py`, `values.py`, `errors.py`, `rules.py` |
| `src/orchestrator/application/` | `ports.py`, `dto.py`, `validation.py` |
| `src/orchestrator/application/use_cases/` | `submit_query.py`, `get_job.py`, `cancel_job.py`, `get_results.py`, `proxy.py`, `run_job.py`, `worker.py` |
| `src/orchestrator/application/stages/` | `expand.py`, `collect.py`, `analyze.py`, `narrate.py`, `finalize.py` |
| `src/orchestrator/adapters/inbound/http/` | `handlers.py`, `presenters.py`, `deps.py`, `schemas.py`, `app.py` |
| `src/orchestrator/adapters/outbound/postgres/` | `mappers.py`, `job_repository.py`, `result_repository.py`, `idempotency_repository.py` |
| `src/orchestrator/adapters/outbound/grpc/` | `clients.py`, `mappers.py` |
| `tests/` | `conftest.py`, `fakes.py`, `unit/` (80 тестов), `contract/` (28 тестов HTTP), `integration/` (7 тестов на testcontainers) |

## Сервис `services/insight`

| Каталог | Файлы |
|---|---|
| корень | `Dockerfile`, `pyproject.toml`, `README.md` |
| `config/` | `glossary_ru_en.yaml` — словарь ru→en и ключевые слова областей |
| `prompts/` | `insight_v1.j2`, `expand_v1.j2` — шаблоны промптов (sha256 в `prompt_versions`) |
| `schemas/` | `insight_llm_output.schema.json`, `expand_llm_output.schema.json` (нормативные копии) |
| `migrations/` | `0001_init.sql` (нормативная копия, 6 таблиц) |
| `src/insight/` | `main.py`, `config.py` |
| `src/insight/domain/` | `values.py`, `entities.py`, `errors.py`, `grounding.py` |
| `src/insight/application/` | `ports.py`, `dto.py`, `validation.py`, `json_output.py`, `prompt_builder.py`, `provider_chain.py`, `fallback.py` |
| `src/insight/application/use_cases/` | `expand_query.py`, `generate_insight.py`, `get_provider_status.py` |
| `src/insight/adapters/inbound/` | `grpc_server.py`, `mappers.py`, `http_ops.py` |
| `src/insight/adapters/outbound/llm/` | `gigachat_provider.py`, `openai_compat_provider.py`, `fake_provider.py` |
| `src/insight/adapters/outbound/` | `postgres/repositories.py`, `metrics.py` |
| `tests/` | `conftest.py`, `fakes.py`, `unit/` (80 тестов), `contract/` (9 тестов gRPC), `integration/` (6 тестов на testcontainers) |

## Сервис `services/ui` (аналитическая панель)

React 19 + TypeScript 5.7 + Vite 6, маршрутизация React Router 7, запросы через TanStack Query 5,
собственная дизайн-система на CSS. Статику раздаёт nginx, он же проксирует `/api` на
orchestrator-api и подставляет `X-API-Key` на сервере.

| Каталог | Файлы |
|---|---|
| корень | `Dockerfile`, `package.json`, `tsconfig*.json`, `vite.config.ts`, `index.html`, `README.md` |
| `deploy/` | `nginx.conf.template`, `entrypoint.sh` |
| `src/` | `main.tsx`, `App.tsx` |
| `src/api/` | `types.ts` (типы контракта), `client.ts` (вызовы и сообщения об ошибках) |
| `src/lib/` | `format.ts` (русские названия и форматы), `report.ts` (отчёт Markdown), `style.ts` (CSS-переменные, режим прокрутки), `useSize.ts` (размер элемента) |
| `src/components/` | `ui.tsx` (баннеры, шкала оценки, скелетоны), `signals.tsx` (прогресс, обозреватель сигналов, исключённые, признаки, источники), `timeline.tsx` (журнал задания), `SignalField.tsx` (анимированное поле сигналов), `icons.tsx`, `Logo.tsx` |
| `src/pages/` | `WelcomePage`, `QueryPage`, `ResultsPage`, `InsightPage`, `ModelPage`, `MethodologyPage` |
| `src/styles/` | `app.css` — токены, темы, раскладки, компоненты, анимации |
| `src/assets/fonts/` | шрифты Jura и Lato (`woff`) с лицензиями OFL |

## ML-конвейер `ml/`

| Каталог | Файлы |
|---|---|
| корень | `Dockerfile`, `pyproject.toml`, `README.md`, `leak_patterns.yaml` |
| `src/ml/` | `cli.py`, `config.py`, `ports.py`, `dataset.py`, `features.py`, `negatives.py`, `enrich.py`, `eda.py`, `leak_check.py`, `train.py`, `export.py`, `report.py`, `evaluate.py` |
| `src/ml/adapters/` | `embedders.py` (e5 из analyzer + хеширующий), `collector_grpc.py`, `tracker.py` (MLflow/JSONL), `model_store_fs.py` |
| `data/raw/` | `dataset_normalized.csv`, `dataset_profile.json` |
| `data/labels/` | `negatives_dev_b.yaml` (125 кандидатов), сгенерированные `negatives_dev_b.jsonl`, `annotations_dev_b.csv`, `dataset_ds-2026.09.16-v1.jsonl` |
| `lexicons/` | `README.md` — единственный экземпляр лексиконов лежит в конфигурации analyzer |
| `reports/` | `eda.{json,md}`, `runs.jsonl`, `style_diagnostic.json`, графики калибровки и матрицы ошибок |
| `tests/` | `conftest.py`, `test_dataset.py`, `test_features_parity.py`, `test_train_smoke.py` — 44 теста |

## Артефакты релиза `releases/`

`wsclf-2026.09.16-2.manifest.json` и `wsclf-2026.09.16-2.rules.json` — манифест и пороги
обученной версии. Сами `joblib`-артефакты в поставку не включены (§22 задания): они
воспроизводятся командой `python -m ml.cli train`.

## Инструменты `tools/`

| Файл | Назначение |
|---|---|
| `gen_proto.sh` | генерация стабов protobuf в `libs/ws_contracts/src` |
| `check_proto_conformance.py` | статическая сверка кода с `.proto`: поля сообщений и перечисления у четырёх сервисов, состав RPC у collector, analyzer и insight |
| `check_sql_conformance.py` | статическая сверка SQL четырёх схем (collector, orchestrator, insight, analyzer) с их DDL |
| `check_ui_conformance.py` | сверка интерфейса с OpenAPI: вызовы путей, импорты, обязательные поля источников и разделы инсайта, отсутствие сырого HTML, русскость текстов |
| `minischema.py` | минимальный валидатор JSON Schema (используется ML-конвейером) |
| `minischema.py` | нормативный минимальный валидатор JSON Schema (используется при сборке набора) |

## Документация `docs/`

| Файл | Содержание |
|---|---|
| `docs/collector/TEST_REPORT.md` | фактические проверки collector |
| `docs/collector/DECISIONS.md` | журнал решений и противоречий collector |
| `docs/analyzer/TEST_REPORT.md` | фактические проверки analyzer и перечень непроверенного |
| `docs/analyzer/DECISIONS.md` | журнал решений и противоречий analyzer (C-A1…C-A4, D-A1…D-A13) |
| `docs/ml/TEST_REPORT.md` | фактические проверки ML-конвейера и перечень непроверенного |
| `docs/ml/DECISIONS.md` | журнал решений ML-конвейера (C-M1…C-M3, D-M1…D-M10) |
| `docs/ml/evaluation_report.md` | отчёт об оценке модели: метрики, ДИ, опорные решения, ограничения |
| `docs/ml/labeling_protocol.md` | протокол разметки отрицательного класса и его фактическое исполнение |
| `docs/orchestrator/TEST_REPORT.md` | фактические проверки orchestrator и перечень непроверенного |
| `docs/orchestrator/DECISIONS.md` | журнал решений orchestrator (C-O1…C-O3, D-O1…D-O9) |
| `docs/api/orchestrator.openapi.yaml` | нормативный контракт HTTP API |
| `docs/insight/TEST_REPORT.md` | фактические проверки insight и перечень непроверенного |
| `docs/insight/DECISIONS.md` | журнал решений insight (C-I1…C-I3, D-I1…D-I10) |
| `docs/ui/TEST_REPORT.md` | фактические проверки интерфейса и перечень непроверенного |
| `docs/ui/DECISIONS.md` | журнал решений ui (C-U1…C-U2, D-U1…D-U7), включая переход со Streamlit на React |

## Как проверить комплект

```bash
# юнит-тесты всех компонентов без сети, БД и тяжёлых моделей
PYTHONPATH=services/collector/src:libs/ws_common/src python -m unittest discover -s services/collector/tests/unit -t .
PYTHONPATH=services/analyzer/src:libs/ws_common/src  python -m unittest discover -s services/analyzer/tests/unit  -t .
PYTHONPATH=ml/src:services/analyzer/src:libs/ws_common/src python -m unittest discover -s ml/tests -t ml
PYTHONPATH=services/orchestrator/src:libs/ws_common/src python -m unittest discover -s services/orchestrator/tests/unit -t .
PYTHONPATH=services/orchestrator/src:libs/ws_common/src python -m unittest discover -s services/orchestrator/tests/contract -t .
PYTHONPATH=services/insight/src:libs/ws_common/src python -m unittest discover -s services/insight/tests/unit -t .
python tools/check_ui_conformance.py

# статические чекеры контрактов и SQL
python tools/check_proto_conformance.py
python tools/check_sql_conformance.py

# полный набор в рабочем контуре
uv run pytest services/collector/tests services/analyzer/tests services/orchestrator/tests services/insight/tests ml/tests
```
