# Изменения контрактов

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
