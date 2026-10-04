# Этап 2: трассировка этапов, режим А и сопоставление с экспертным списком

**Исходное состояние:** архив `weak_signals_stable.zip`, SHA-256 `100b2be2…7663` — состояние после этапа 1.
**План:** итоговый план этапов (STAGES_AND_STAGE1) и аудит (`AUDIT_REPORT.md`, разделы 7, 10–12; `AUDIT_REGISTRY.md`).
Во второй этап входят:
- A3 — трассировка этапов и режим А;
- A4 — протокол и инструмент сопоставления с экспертным списком;
- B2-a — проверка «не опрошен» против «пусто»;
- B4 — гипотеза, на этом этапе только измерение; решение — на этапе 3.

Semantic Scholar Bulk Search (B3) — этап 3, здесь не начат.

**Решение владельца от 03.10.2026:** промпты и ответы LLM сохраняются целиком. Это заменяет правило §14.5 ТЗ.

## 1. Выполнение плана

| ID | Что изменено | Где | Проверка | Результат | Приёмка |
|---|---|---|---|---|---|
| A3 — трассировка | **collector:** итог каждого запроса к источнику и событие для каждого документа: `ACCEPTED`, `DEDUP`, `SOURCE_CAP`, `REJECTED` — с источником и фразой запроса. **analyzer:** воронка документов — почти-дубли, порог близости, одиночные кластеры, кластеры, отсечённые числом кандидатов. **orchestrator:** трасса стадий `job`, `expand`, `collect`, `analyze`, `narrate` / `narrate_legacy` — пул, вердикты, вероятности, раунды доводки, отказы с причинами, остановка по сроку, показанные карточки | collector `trace_recording.py`, `sources/base.py`, `run_collection.py`, `trace_repository.py`, миграция 0008; analyzer `capture.py`, `run_analysis.py`; orchestrator `narrate_rubric.py`, `narrate.py`, `run_job.py`, `result_repository.py`, миграция 0003 | `test_collection_trace.py` (6), `test_job_trace.py` (4), `test_benchmark_replay.py`: в живом прогоне записаны все границы и все ключи трассы | Каждый документ, выданный адаптером, получает событие (6 из 6 в тесте). Ошибки 429 и таймаута записываются с кодом. Сбой записи не обрывает сбор и задание | Принято в коде. На стенде полноту показывает `freeze.py`: в `issues` снимка не должно быть строк «нет трассы» |
| A3 — режим А, запись | Записываются: сырые ответы источников (сжатые, без ключей и заголовков); документы коллекции с хешами; векторы документов и всех вызовов эмбеддера в порядке вызова; ответы Wikipedia; дата признаков; настройки; полные промпты и ответы LLM с ключом повтора и `job_id`. В снимок также попадают версии (git HEAD, образы compose, версии промптов, модели LLM), `.env` без секретов, время сбора, SHA-256 каждого файла | analyzer `capture_repository.py`, миграция 0002; insight `llm_recording.py`, `provider_chain.py`, `repositories.py`, миграция 0003; `ws_common/ids.py`, `grpc_interceptors.py`; `tools/benchmark/freeze.py` | `test_llm_recording.py` (3), `CaptureTest` (2), `FreezeToolTest` (1), `FindSnapshotsTest` (1) | Ключ повтора зависит только от запроса. Записываются и неуспешные попытки провайдеров. Векторы восстанавливаются без потери разрядов. Секреты `.env` маскируются | Принято в коде. На стенде — `freeze.py` по всем темам |
| A3 — режим А, повтор | `python -m ml.benchmark replay`: анализ, рубрика, доводка и выдача на снимке без сети, тем же кодом сервисов. Сравнение с живой выдачей, повтор дважды, абляции через `--set` | `ml/src/ml/benchmark/` (`replay.py`, `memory.py`, `snapshot.py`, `cli.py`); общие функции запросов insight — orchestrator `grpc/mappers.py`, insight `inbound/rpc.py`; analyzer `collector_mappers.py`; образ trainer | `test_benchmark_replay.py` (9), `test_benchmark_grpc_parity.py` (1) | Повтор записанного прогона — `IDENTICAL`, обращений без записи 0, два повтора с одинаковым хешем. Абляция с новыми запросами к LLM — `INCOMPLETE` со списком ключей. Испорченный файл даёт «не совпала SHA-256», отсутствующий — явную остановку. Запись не меняет выдачу. Настоящий gRPC-клиент orchestrator и настоящий сервер insight дают те же карточки, вердикты и запросы к модели, что и повтор | **Критерий «повтор одной версии даёт ту же выдачу» выполнен на тестовых данных.** На снимках стенда — команда `replay` из раздела 7, ожидается `IDENTICAL`. Мной не выполнено: нет стенда и Docker |
| A4 | Протокол `match-v1`: совпадение по ссылке (URL, DOI, arXiv) засчитывается автоматически; по термину или синониму — спорное, решают два человека. Знаменатель — экспертные сигналы темы. Листы для двух проверяющих, каппа Коэна с интервалом, разрешение разногласий, полнота по этапам, первый этап потери. В `human_eval` — precision@K с бутстрэпом по темам | `ml/benchmark/expert_match.py`, `stats.py`; `docs/benchmark/MATCHING_PROTOCOL.md`; `tools/quality/human_eval.py` | `ExpertMatchTest` (3), `StatsTest` (1) | Ссылки нормализуются (`abs`/`pdf`, `10.48550/arXiv.*`). Неразмеченные и спорные пары не засчитываются. Тема вне кейса помечается | Инструмент и протокол приняты. «Согласие двух проверяющих» требует людей — до разметки спорных пар заблокировано |
| B2-a | Итог опроса источника по данным запуска адаптера и ответов | `ml/benchmark/funnel.py` | `SourceOutcomeTest`, `FunnelTest` | **Найдено:** адаптер, не сделавший ни одного запроса, получает статус `COMPLETED`, и по статусу его не отличить от «опрошен, пусто». В отчёте воронки итог «не опрошен» определяется по нулю HTTP-запросов, отдельно от «опрошен, пусто», «отказ», «лимит частоты», «не завершён», «частично» | Проверено. Изменение статусов в самом collector — не в объёме этапа |
| B4 — измерение | В отчёте воронки: кандидаты ниже порога близости кандидата (`below_candidate_query_sim`), кластеры, отсечённые числом кандидатов, кандидаты вне пула, в том числе с `LOW_QUERY_RELEVANCE` | `ml/benchmark/funnel.py` | `FunnelTest` | Поля считаются по снимку | Измерение готово, значения — по снимкам стенда. Решение — этап 3 |
| Предпосылки | `ws_common.jsonsafe`: NaN, перечисления и кортежи — в jsonb. `tools/check_sql_conformance.py` читает все миграции, раньше читал только `0001` и не видел новых таблиц | `libs/ws_common/src/ws_common/jsonsafe.py`, `tools/check_sql_conformance.py` | `test_json_safe_replaces_values_postgres_rejects`; проверка SQL — PASS | — | Принято |

## 2. Файлы

69 файлов (новые помечены «+») и этот отчёт.

- **collector:**
  - `migrations/0008_collection_trace.sql` (+);
  - `application/trace_recording.py` (+);
  - `adapters/outbound/postgres/trace_repository.py` (+);
  - `application/ports.py`, `application/use_cases/run_collection.py`;
  - `adapters/outbound/sources/base.py`, `config.py`, `main.py`;
  - `tests/unit/test_collection_trace.py` (+).
- **analyzer:**
  - `migrations/0002_analysis_capture.sql` (+);
  - `application/capture.py` (+);
  - `adapters/outbound/postgres/capture_repository.py` (+);
  - `adapters/outbound/collector_mappers.py` (+);
  - `application/ports.py`, `application/use_cases/run_analysis.py`;
  - `adapters/outbound/collector_grpc.py`, `config.py`, `main.py`.
- **orchestrator:**
  - `migrations/0003_job_traces.sql` (+);
  - `application/ports.py`, `application/stages/narrate.py`, `application/stages/narrate_rubric.py`;
  - `application/use_cases/run_job.py`;
  - `adapters/outbound/grpc/mappers.py`, `adapters/outbound/grpc/clients.py`;
  - `adapters/outbound/postgres/result_repository.py`;
  - `tests/fakes.py`, `tests/unit/test_job_trace.py` (+).
- **insight:**
  - `migrations/0003_llm_recordings.sql` (+);
  - `application/llm_recording.py` (+);
  - `adapters/inbound/rpc.py` (+);
  - `application/ports.py`, `application/provider_chain.py`;
  - `adapters/inbound/grpc_server.py`, `adapters/outbound/postgres/repositories.py`;
  - `config.py`, `main.py`;
  - `tests/unit/test_llm_recording.py` (+).
- **ws_common:** `ids.py`, `grpc_interceptors.py`, `jsonsafe.py` (+).
- **ml:**
  - `Dockerfile`;
  - `src/ml/benchmark/` (+): `__init__`, `__main__`, `cli`, `snapshot`, `memory`, `replay`, `funnel`, `expert_match`, `stats`;
  - `tests/test_benchmark_replay.py` (+), `tests/test_benchmark_tools.py` (+), `tests/test_benchmark_grpc_parity.py` (+).
- **Инструменты:** `tools/benchmark/freeze.py` (+), `tools/quality/human_eval.py`, `tools/check_sql_conformance.py`.
- **Документация и настройки:**
  - `docs/benchmark/MODE_A.md` (+), `docs/benchmark/MATCHING_PROTOCOL.md` (+), `docs/STAGES.md` (+);
  - `docs/stages/STAGE2_REPORT.md` (+);
  - `docs/{collector,analyzer,orchestrator,insight}/DECISIONS.md`, `docs/config_dictionary.md`;
  - `OVERVIEW.md`, `CHANGELOG_CONTRACTS.md`, `.env.example`, `.gitignore`.

## 3. До и после

| Набор тестов | До | После |
|---|---|---|
| collector | 242 OK, 1 пропущен | 247 OK, 1 пропущен |
| analyzer | 218 OK | 218 OK |
| orchestrator, модульные | 138 OK | 142 OK |
| orchestrator, контрактные | 28 OK | 28 OK |
| insight | 156 OK | 159 OK |
| ml | 92 OK | 112 OK, 1 пропущен в песочнице: тест пути gRPC требует пакета grpc и выполняется в образе trainer |
| Соответствие proto / SQL / UI | PASS | PASS (проверка SQL теперь охватывает все миграции) |

- **Поведение выдачи не изменилось.** Тест `test_recording_does_not_change_output` даёт ту же выдачу с записью и без неё. Контракты gRPC и HTTP API не менялись, в базе только добавлены таблицы. Этап не улучшает качество выдачи: он делает её измеримой.
- **Существующие тесты не менялись.** Единственная правка — в `services/orchestrator/tests/fakes.py`: в дублёр хранилища результатов добавлен новый метод порта `save_trace`.

## 4. Миграции

Применяются сервисами при старте: collector 8, analyzer 2, orchestrator 3, insight 3. Прежние таблицы не меняются.
Откат кода через `git reset` таблицы этапа 2 не удаляет. Прежний код их не использует.

## 5. Проверки

**Выполнено:**
- все модульные и контрактные тесты (раздел 3);
- проверки соответствия proto, SQL и UI;
- компиляция всех изменённых модулей;
- грубая статическая проверка неиспользуемых импортов и неопределённых имён в изменённых файлах;
- применение патча к чистой распаковке исходного архива, совпадение дерева и повтор всех тестов на нём;
- тест пути gRPC в песочнице — с заглушкой модуля `grpc`, которая нужна только для импорта (пакета нет, сети нет). В образе trainer этот тест идёт с настоящим `grpc`.

**Не выполнено, с причиной:**
- сборка образов, запуск стенда, интеграционные тесты с PostgreSQL, прогон тем, выгрузка и повтор реальных снимков — в среде разработки нет Docker, PostgreSQL и сети. Всё это делают команды раздела 7;
- `ruff` и `mypy` — пакеты не установлены, сети нет;
- импорт `ws_common.grpc_interceptors` в песочнице — нет `structlog` и `grpc`; проверяется запуском сервисов.

## 6. Ограничения и риски

Подробно — `docs/benchmark/MODE_A.md`, раздел «Известные ограничения». Кратко:
- Повтор начинается с документов коллекции. Сбор и расширение запроса не повторяются.
- Состояние цепочки LLM между заданиями и кеш нарративов insight не записываются. Если они повлияли на задание, повтор получит `INCOMPLETE`, а не другую выдачу.
- Задания, выполненные до этапа 2, записей не имеют: выгрузка отмечает неполноту.
- Записи этапа 2 хранятся без автоматической очистки. Объём на задание на стенде не измерялся.
- Сопоставление по терминам требует разметки двумя людьми. До неё полнота по экспертному списку — нижняя граница.

## 7. Применение локально (без пуша)

Каталог проекта — `/Users/zorinmihail/Desktop/weak-signals`, патч — `~/Downloads/ws-stage2.patch`.

```zsh
cd /Users/zorinmihail/Desktop/weak-signals
git add -A && git commit -q -m "local: before stage2" || echo "нечего коммитить"
git tag -f before-stage2
git apply --check ~/Downloads/ws-stage2.patch && git apply ~/Downloads/ws-stage2.patch && echo "ПАТЧ ПРИМЕНЁН"
docker compose up -d --build
docker compose --profile ml build trainer
for i in {1..60}; do curl -sf http://127.0.0.1:8080/readyz >/dev/null && break; sleep 10; done; curl -s -o /dev/null -w "readyz %{http_code}\n" http://127.0.0.1:8080/readyz
docker compose exec -T postgres psql -U postgres -d weaksignals -At -c "SELECT s, max(version) FROM (SELECT 'collector' s, version FROM collector.schema_migrations UNION ALL SELECT 'analyzer', version FROM analyzer.schema_migrations UNION ALL SELECT 'orchestrator', version FROM orchestrator.schema_migrations UNION ALL SELECT 'insight', version FROM insight.schema_migrations) m GROUP BY s ORDER BY s"
docker compose --profile ml run --rm --entrypoint python trainer -m unittest discover -s ml/tests -t ml 2>&1 | tail -3
python3 tools/quality/stand_report.py --no-checks --all-topics
RUN="$(ls -t ml/reports/stand/stand-run-*.json | head -1)" && echo "$RUN"
python3 tools/benchmark/freeze.py --run-file "$RUN" --out ml/reports/benchmark
SNAP="$(ls -td ml/reports/benchmark/*/ | head -1)" && echo "$SNAP"
docker compose --profile ml run --rm --entrypoint python trainer -m ml.benchmark verify "/app/$SNAP"
docker compose --profile ml run --rm --entrypoint python trainer -m ml.benchmark replay "/app/$SNAP" --out "/app/${SNAP}replay"
docker compose --profile ml run --rm --entrypoint python trainer -m ml.benchmark funnel "/app/$SNAP" --out "/app/${SNAP}funnel"
docker compose --profile ml run --rm --entrypoint python trainer -m ml.benchmark match "/app/$SNAP" --dataset /data/labels/dataset_ds-2026.09.19-v2.jsonl --out "/app/${SNAP}match"
git add -A && git commit -q -m "local: stage2 tracing and mode A" && git tag -f stage2-local && echo "ЭТАП 2 ЗАФИКСИРОВАН"
```

**Что ожидается:**
- строка `ПАТЧ ПРИМЕНЁН`;
- `readyz 200`;
- версии миграций: `analyzer|2`, `collector|8`, `insight|3`, `orchestrator|3`;
- тесты ML: `Ran 112 tests` и `OK (skipped=1)` — пропуск ожидаем: в образе trainer нет журналов прогонов v1 для `test_seq_pipeline`;
- `freeze` — «полный» по каждому заданию;
- `verify` — `OK` по каждому снимку;
- `replay` — `IDENTICAL`, «совпадают между собой: True»;
- `funnel` — таблица воронки;
- `match` — число спорных пар и листы `reviewer_1.csv` / `reviewer_2.csv`.

**Откат:**

```zsh
cd /Users/zorinmihail/Desktop/weak-signals && git reset -q --hard before-stage2 && docker compose up -d --build
```
