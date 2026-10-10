# Изменения контрактов

## 2026-10-07 — v18: этап 4 — поиск «от темы к технологиям»
- proto:
  - `insight.v1.ExpandQueryResponse.technologies = 6` — 0..40 конкретных технологий (англ. названия);
  - `collector.v1.SearchTerms.technologies = 3` — технологии для прицельного прохода.
  Сгенерированные модули обновлены тем же дескриптором, что даёт `protoc`.
- insight: prompt `expand_v2` и схема `expand_v2_llm_output.schema.json` — список `technologies` необязателен,
  без него расширение не уходит в резерв; миграция `0004_expansion_technologies.sql`.
- collector:
  - миграция `0010_collection_technologies.sql`: `terms_technologies`, `technologies_mined`;
  - `SourceAdapter.search_targeted` у arXiv, Semantic Scholar (`/paper/search`), OpenAlex, GitHub, Hacker News;
  - сбор в два прохода, извлечение технологий `domain/technology_mining.py`;
  - бюджет Semantic Scholar 50 запросов, Hacker News — 60.
- analyzer: `singleton_candidates` — ближайшие к запросу одиночные документы остаются кандидатами.
- orchestrator: `WS_COLLECT_TIME_BUDGET_SECONDS` по умолчанию 240.
- GDELT не менялся.

## 2026-10-06 — v17: этап 3.1 — Hacker News вместо GDELT, Semantic Scholar по всем словам
- proto `common.v1.SourceKey`: новое значение `SOURCE_KEY_HACKERNEWS = 12`. Сгенерированный `common_pb2.py`
  обновлён тем же дескриптором, что даёт `protoc`; при сборке образов код генерируется заново из proto.
- collector:
  - миграция `0009_hackernews.sql` — источник `hackernews` в каталоге, «Hacker News (Search by Algolia)», 1 запрос/с;
  - адаптер `HackerNewsAdapter`: `GET hn.algolia.com/api/v1/search`, `tags=story`, окна дат `created_at_i`;
  - правило `by_domain` в `trust_rules.yaml`: тип документа агрегатора — по домену ссылки, иначе умолчание источника;
  - Semantic Scholar bulk: запрос — все слова фразы без кавычек.
- Новая переменная `WS_HACKERNEWS_HISTORY_MONTHS`; политика по умолчанию Hacker News — 18 запросов, 1 повтор, пауза 10 с.

## 2026-10-05 — v16: этап 3 — источники, бюджет и паузы, Semantic Scholar Bulk, доводка
- gRPC-контракты, HTTP API и миграции не менялись.
- **collector, GDELT:**
  - окна истории по 90 дней через `STARTDATETIME`/`ENDDATETIME` вместо `timespan=3months`;
  - пауза только после 429 или текста вместо JSON, 120 с вместо 600, ожидается, если кончается до срока сбора;
  - таймаут соединения 20 с, один повтор окна;
  - отказы отдельных окон видны в статусе запуска.
- **collector, Semantic Scholar:** `/paper/search/bulk`, `sort=publicationDate:desc`, окно 24 месяца, продолжение по `token`.
- **collector, B2-b:** политика источника — бюджет запросов на сбор, повторы после 429, пауза без `Retry-After`.
  Новый параметр `AdapterFailure.retryable`, у `HttpxClient` — `connect_timeout_seconds`.
- **insight, доводка:** один повтор отклонённой карточки с причиной отказа (ключ `исправление` в данных карточки).
  Системный промпт `finalize_v5` не менялся.
- **ml:** эксперимент B1 (`--b1-report`), экспортируемая модель не менялась.
- **Новые переменные:** `WS_GDELT_HISTORY_MONTHS`, `WS_GDELT_CONNECT_TIMEOUT_SECONDS`,
  `WS_SEMANTIC_SCHOLAR_WINDOW_MONTHS`, `WS_COLLECTOR_SOURCE_POLICY`.

## 2026-10-04 — v15: этап 2 — трассировка и режим А (аудит A3, A4, B2-a)
- gRPC-контракты (proto) и HTTP API не менялись.
- Новые таблицы. Миграции применяют сервисы при старте:
  - collector — `raw_responses`, `document_events` (0008);
  - analyzer — `analysis_captures` (0002);
  - orchestrator — `job_traces` (0003);
  - insight — `llm_recordings` (0003).
- insight: полные тексты промптов и ответов LLM сохраняются в `llm_recordings`. Это решение владельца проекта от
  03.10.2026 вместо правила §14.5 ТЗ. Журнал `llm_calls` не менялся.
- ws_common: входящий correlation id gRPC записывается в контекст сервиса. Поэтому исходящие вызовы и записи LLM несут
  `job_id` задания.
- orchestrator, insight: построение запросов к insight и разбор ответов вынесены в чистые функции
  (`orchestrator.adapters.outbound.grpc.mappers`, `insight.adapters.inbound.rpc`). Их вызывают gRPC-клиент и сервер;
  поведение не изменилось. На эти же функции опирается повтор режима А.
- analyzer: функция `common.v1.Document` → `DocumentRef` вынесена в `collector_mappers.py`.
- Новые переменные, все по умолчанию `true`: `WS_COLLECTOR_TRACE_ENABLED`, `WS_ANALYZER_CAPTURE_ENABLED`,
  `WS_LLM_RECORD_ENABLED`.
- Образ trainer: добавлены код collector, orchestrator и insight, `tools/benchmark` и Jinja2 — для повтора режима А и его тестов.
- Инструменты:
  - `tools/benchmark/freeze.py`;
  - `python -m ml.benchmark` с командами verify, replay, funnel, synonyms-template, match, match-score, compare;
  - `tools/quality/human_eval.py score --k` — precision@K.
- `tools/check_sql_conformance.py` читает все миграции сервиса, а не только `0001`.
- Повтор режима А идёт от кандидатов живого analyzer. Выгрузка сохраняет `candidate_rows/features/documents.jsonl`,
  прочитанные так же, как их читает analyzer для orchestrator. Пересчёт анализа сверяется отдельно.

## 2026-09-29 — v14: точность и объём выдачи
- orchestrator: включено правило «обзоры — не меньше половины источников → не показывать» — в предфильтре по
  источникам кандидата и по источникам готовой карточки (датасет B, 234 карточки: −31 негатив, −2 позитива,
  точность 0,248 → 0,279); такие кандидаты не возвращаются и через резервную ветку. Пул рубрики `WS_RUBRIC_POOL`
  40 → 80 (допустимо 5..100); в режиме rubric столько же кандидатов запрашивается у analyzer
  (`AnalysisParams.max_candidates`, поле уже было в контракте); рубрика идёт вызовами по 20 кандидатов, между ними
  проверяется срок задания.
- insight: `judge_v6` — финальная проверка готовой карточки (`JudgeCandidatesRequest.mode = "rubric_final"`): для
  запросов о безопасности систем ИИ по теме только защита самих моделей и ИИ-систем; безопасность применения ИИ в
  прикладных областях и обнаружение созданного ИИ контента — N-OFF. Текст первой рубрики не изменился.
  `finalize_v5` — у каждого источника карточки список допустимых терминов из заголовка (`terms`) для скобок в названии.
- collector: рекламные материалы («Sponsored:», «Партнёрский материал», «На правах рекламы» в заголовке или тексте)
  не попадают в коллекцию (`advertising_markers` в `config/trust_rules.yaml`); OpenAlex — публикации с 2024 года
  (было 2023; граница для остальных источников не менялась).
- Схема proto не менялась: `mode` — строковое поле, новое значение описано в комментарии `insight.proto`.

## 2026-09-29 — v13: версия модели в «Происхождении»
- orchestrator: `provenance.model_version_id` в ответе о карточке заполняется версией модели analyzer из статистики
  задания (раньше поле всегда было пустым). Контракт API не менялся: поле уже было в схеме.

## 2026-09-29 — v12: таймаут LLM доходит до SDK
- insight: WS_LLM_TIMEOUT_SECONDS передаётся в SDK GigaChat (параметр `timeout` или GIGACHAT_TIMEOUT) и в HTTP-клиент
  резервных провайдеров (раньше жёстко 60 с). Стенд 29.09: SDK обрывал ответы GigaChat-2-Max с ReadTimeout с
  интервалом ~30 с, хотя таймаут в настройках был 300 с. Контракт proto не менялся.

## 2026-09-29 — v11: таймауты LLM 300 с, проверка термина по смыслу
- insight: таймаут вызова LLM 300 с, общий бюджет 600 с (стенд: ReadTimeout в 10 из 17 вызовов доводки на
  GigaChat-2-Max); термин в названии засчитывается дословно или по 60 % основ значимых слов в выбранных источниках.
- orchestrator: сроки RPC к insight 330–660 с. .gitattributes: CSV-файлы данных хранятся байт в байт.
  Контракт proto не менялся.

## 2026-09-29 — v10: приведение к сдаче (без изменения отбора)
- orchestrator/ui: счётчик «уверенность выше 75 %» считает показанные карточки, подпись «среди найденных слабых сигналов».
- RUN_LOCAL.md (Linux, macOS, Windows), ссылки из README; .gitattributes (LF), .gitignore для результатов прогонов;
  WS_SEMANTIC_SCHOLAR_API_KEY в .env.example. Контракт proto не менялся.

## 2026-09-29 — v9: источники не падают от медленных ответов и одной плохой ленты; больше карточек R
- collector: arXiv без 30-минутной паузы после таймаута; arXiv и GDELT — таймаут чтения 45 с (отдельные клиенты);
  RSS: неразборчивый ответ одной ленты не обрывает остальные.
- insight: доводка по одной карточке; orchestrator: 3 раунда замены, уверенность с двумя знаками (полоса, счётчик
  «> 75 %» и интерфейс считают по одному значению); отклонённые финальной проверкой не возвращаются через резервную
  ветку; срок RPC расширения запроса 60 с (GigaChat-2-Max).
- ws_common/ml: признаки локального классификатора учитывают отрицание («нет массового внедрения»); модель
  переобучена: organizers_cv accuracy 0,907, F1 0,893, каждый фолд ≥ 0,84.
- tools: stand_report не тратит лимит arXiv/GDELT живой проверкой перед прогоном (флаг --check-rate-limited).
- Документация: OVERVIEW.md — архитектура, конвейер, методика решений, модели, карта docs/. Контракт proto не менялся.

## 2026-09-29 — v8: надёжное расширение запроса, правдивые показатели
- insight: возвращён промпт `expand_v1` (на нём получены все результаты v4–v6a: промпт `expand_v2` отклонялся схемой,
  а на Mac кеш по тексту запроса подставлял ответы `expand_v1`); кеш учитывает версию промпта и не хранит резервные
  расширения; причина отказа пишется в журнал; модель по умолчанию GigaChat-2-Max.
- orchestrator: добор до минимума только из U; счётчик «уверены > 75 %» — по показанной уверенности.
- ws_common: httpx/httpcore не пишут адреса запросов (в них бывают ключи API). UI: «Методология» и стартовая страница
  описывают текущий конвейер. Контракт proto не менялся.

## 2026-09-28 — v6a: чистый фильтр источников карточки (правка A1)
- insight: после всех проверок доводки v6 в карточке остаются источники с общим значимым словом с термином и
  латиницей названия, источник кейса и русские тексты; карточка не отклоняется, минимум один источник остаётся.

## 2026-09-28 — v6: карточки по своим источникам, заметки СМИ не склеиваются, финальная проверка
- insight: `finalize_v4` (выбор источников `source_ids`, оригинальный термин в скобках в названии), `judge_v5`
  (для «безопасности систем X» по теме только защита самих систем X). Контракт proto не менялся.
- analyzer: `WS_MARKET_NOTES_SEPARATE`; orchestrator: `WS_FINAL_SPECIFICITY_CHECK`, `WS_CONFIDENCE_CALIBRATION`.

## 2026-09-27 — v5: решение рубрики R, порядок — модель; пул без ранних отсечек; близость к поднаправлениям
- orchestrator: показ — кандидаты R рубрики, порядок и уверенность — локальный классификатор; в пул рубрики входят
  исключённые analyzer по `NO_TRUSTED_SOURCE` и `LOW_QUERY_RELEVANCE`; `profile_classifier.json` — модель по профилям.
- analyzer: `WS_QUERY_RELEVANCE_MODE` (mean | max); collector: запрос arXiv — значимые слова через AND;
  insight: промпт `finalize_v3`; признаки классификатора без годов и служебных слов. Контракт proto не менялся.

## 2026-09-27 — v4: классификатор по схеме ТЗ, поиск ниш, рыночные сигналы, arXiv
- `weaksignals.insight.v1`: `JudgeVerdict.technology_ru` (13), `JudgeVerdict.profile_ru` (14) — извлечённая технология
  и профиль по источникам для локального классификатора. Обратно совместимо.
- insight: промпты `judge_v4` и `expand_v2` (поднаправления запроса); orchestrator: решение — локальный классификатор
  `ws_common.signal_classifier` (`services/orchestrator/config/signal_classifier.json`), `WS_RUBRIC_MIN_PROBABILITY`,
  `WS_RUBRIC_MIN_CARDS`; analyzer: `WS_MARKET_RESERVED_CANDIDATES`; collector: образ bookworm, заголовки CDN в отказах.

## 2026-09-27 — рубрика v3: локальная модель решает, выдача не бывает пустой
- orchestrator: ранжирование и уверенность — локальная модель `ws_common.rubric_model` (JSON
  `services/orchestrator/config/rubric_ranker.json`, обучение — `python -m ml.rubric_ranker`); кандидаты ранжируются,
  а не отсекаются по коду рубрики; при отказе доводки — прежний генератор карточек. Настройка `WS_RUBRIC_MODEL_PATH`.
- insight: промпт `judge_v3` (общие формулировки — N-GEN, вес рыночных свидетельств); контракт proto не менялся.
- collector: Роспатент — 10 документов на фразу вместо 25.

## 2026-09-27 — рубрика v2: устойчивость, русская выдача, рыночные сигналы
- insight: промпт `finalize_v2` (все поля на русском, резюме каждого источника); `FinalizedCard.stage/trend` = 0 —
  стадию и тренд задаёт рубричная оценка с калибровкой. Контракт proto не менялся.
- orchestrator: `WS_RUBRIC_LEGACY_FALLBACK`, `WS_RUBRIC_FILL_UNCERTAIN`, `WS_STAGE_CALIBRATION`, `WS_TREND_CALIBRATION`.
- analyzer: `WS_KEEP_MARKET_SINGLETONS`; журнал `analysis.funnel` — документы по типам источников на шагах анализа.

## 2026-09-26 — рубричный отбор и пакетная доводка карточек
- `weaksignals.insight.v1`: `JudgeSource`; поля `JudgeItem.sources`, `JudgeItem.composition_ru`,
  `JudgeCandidatesRequest.mode` ("rubric_v2"), `JudgeVerdict.code/on_topic/concrete/early_stage/verifiable/stage/trend/confidence`;
  RPC `FinalizeCards` (`FinalizeCard`, `FinalizeCardsRequest`, `FinalizedCard`, `FinalizeCardsResponse`). Обратно совместимо.
- orchestrator: `WS_SELECTION_MODE` (legacy | rubric), `WS_RUBRIC_POOL`, `WS_FINALIZE_ENABLED`.

## 2026-09-25 — источник Роспатент
- `weaksignals.common.v1.SourceKey`: добавлено значение `SOURCE_KEY_ROSPATENT = 11` (обратно совместимо).
- collector: миграция `0007_rospatent.sql` (каталог источников), переменная окружения `WS_ROSPATENT_TOKEN`.
- Правила доверенности: у источника может быть флаг `authoritative` — тип адаптера не пересматривается доменными правилами.

## 2026-09-24 — ml-rework v2
- model-store: манифест v2 (`feature_schema_version: "v2"`, `model_family: "logreg_seq_laplace"`, файл `model_v2.json`), схема `schemas/model_manifest_v2.schema.json`; v1 без изменений.
- Окружение: `WS_CANDIDATE_JUDGE_ORDER` (orchestrator, `ml`|`llm`, по умолчанию `ml`), `WS_ML_SCORE_ABLATION` (analyzer, `none`|`constant`|`shuffle`).
- gRPC, SQL и OpenAPI не менялись.

## 1.0.0 — 2026-09-15
Первая версия: weaksignals.common.v1, collector.v1, analyzer.v1, insight.v1; orchestrator OpenAPI 1.0.0.

## 1.1.0 — 2026-09-17
Контракты не менялись. Изменена реализация `ui`: вместо Streamlit — SPA на React
(ТЗ §3.2), контейнер отдаёт статику через nginx и проксирует `/api` на `orchestrator-api`.
В `compose.yaml` у сервиса `ui` заменены `entrypoint` и `healthcheck`; порт 8501 сохранён.

## 1.2.1 — 2026-09-18
Контракты не менялись. Исправлена сборка образов: убрана зависимость от отсутствующего
`uv.lock` (`uv sync` резолвит зависимости при сборке), в deps-стадию добавлены `pyproject.toml`
всех членов рабочего пространства, `grpcio-tools` ставится точечно для генерации стабов,
повторный `uv sync` в runtime-стадии удалён (код находится по `PYTHONPATH`). Версия uv
закреплена на 0.5.11, добавлен корневой `.dockerignore`.

## 1.2.2 — 2026-09-18
Контракты не менялись. Исправлена сборка образа `ui`: убран неиспользуемый импорт `EmptyState`
в `components/signals.tsx` (ошибка `tsc` TS6133 при `noUnusedLocals`), добавлен `@types/node`
и `types: ["node"]` в `tsconfig.node.json` для `process.env` в `vite.config.ts` (TS2580).
`tools/check_ui_conformance.py` дополнен проверкой неиспользуемых импортов, чтобы эта ошибка
ловилась до сборки.
