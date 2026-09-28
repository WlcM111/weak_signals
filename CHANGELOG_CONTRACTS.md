# Изменения контрактов

## 2026-09-28 — v7: больше точных карточек
- insight: `finalize_v5` — список допустимых терминов из заголовков источников, термин подтверждается дословно или
  на 60 % значимых слов, источники без общего значимого слова с термином отбрасываются кодом; доводка по одной
  карточке. orchestrator: 3 раунда замены, финальная проверка явно проверяет зрелость. Контракт proto не менялся.

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
