# Изменения контрактов

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
