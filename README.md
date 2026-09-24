# weak-signals

Сервис автоматизированного поиска слабых сигналов в научно-технологических отраслях
(кейс Газпромбанк.Тех). Пользователь вводит запрос в свободной форме — система собирает
материалы из открытых источников, отделяет зрелые технологии, маркетинговый хайп и
информационный шум, формирует ТОП-15 зарождающихся трендов с объяснением каждого решения,
кейс-примерами и проверяемыми источниками.

## Состав

| Компонент | Что делает | Порты |
|---|---|---|
| `collector` | сбор документов из открытых источников: OpenAlex (нужен бесплатный ключ), arXiv, RSS-ленты, GitHub; Wikipedia — индикатор зрелости. Адаптеры PatentsView и hh отключены: PatentSearch API остановлен с 20.03.2026 (миграция в USPTO ODP), условия API hh не подтверждены | 50051 / 8081 |
| `analyzer` | 25 интерпретируемых признаков, 8 правил исключения, калиброванная модель, ранжирование | 50052 / 8082 |
| `insight` | русскоязычные нарративы и резюме источников через LLM с проверкой обоснованности | 50053 / 8083 |
| `orchestrator` | HTTP API, очередь заданий, снимок результата ТОП-N | 8080 / 8084 |
| `ui` | аналитическая панель на React: запрос, выдача, инсайты, модель, методология | 8501 |
| `ml` (trainer) | подготовка данных, обучение, честная оценка и экспорт модели | — |

Стек: Python 3.12, PostgreSQL 18, gRPC между сервисами, FastAPI на входе, React 19 + TypeScript +
Vite в интерфейсе, scikit-learn и e5-эмбеддинги в модели, GigaChat / YandexGPT для нарративов.

---

## Быстрый старт (Docker, полный контур)

Требуется Docker с Compose, ~6 ГБ места и интернет при первом запуске.

```bash
# 1. Ключи и настройки
cp .env.example .env
#    Минимум для работы без LLM: ничего заполнять не нужно.
#    Для генеративных нарративов задайте один из вариантов:
#      WS_GIGACHAT_CREDENTIALS=<авторизационные данные GigaChat>
#      или WS_YANDEX_API_KEY=<ключ> и WS_YANDEX_FOLDER_ID=<каталог>
#    Для публичного стенда обязательно задайте WS_API_KEY=<произвольная строка>.

# 2. Поднять инфраструктуру и сервисы
docker compose up -d --build postgres collector analyzer insight orchestrator-api orchestrator-worker ui

# 3. Обучить модель (до обучения задания завершаются отказом MODEL_NOT_LOADED)
docker compose --profile ml run --rm trainer build-dataset
docker compose --profile ml run --rm trainer train        # скачает e5 (~1.1 ГБ), ~10 мин на CPU
#    analyzer подхватит новую модель сам в течение 15 секунд — перезапуск не нужен

# 4. Проверить готовность
docker compose ps
curl -fs http://127.0.0.1:8080/readyz && echo " — API готов"

# 5. Открыть интерфейс
#    http://localhost:8501         — аналитическая панель
#    http://127.0.0.1:8080/docs    — документация API
```

Первый запрос: откройте панель, введите направление (например, «слабые сигналы в области
кибербезопасности»), нажмите «Найти слабые сигналы». Страница результата обновляется сама:
видно стадию, счётчики и появляющиеся карточки. Клик по технологии открывает инсайт —
документ-отчёт с описанием, преимуществом, кейс-примером, полной таблицей признаков и
источниками; отчёт можно скачать в Markdown.

Остановить: `docker compose down`. Удалить данные: `docker compose down -v`.

### Что доступно без ключей LLM

Всё, кроме генеративных текстов. `insight` вернёт экстрактивный результат: описание и
кейс-пример собираются из найденных источников и помечаются в интерфейсе как «инсайт
экстрактивный», задание завершится статусом `PARTIAL`. Скоринг, ключевые предикторы, правила
исключения, источники и методология работают в полном объёме.

---

## Быстрый старт без Docker (проверка кода за 5 минут)

Нужен только Python 3.12 и зависимости из `requirements-local.txt` — ни сети, ни БД.

```bash
pip install -r requirements-local.txt
export PYTHONPATH=ml/src:services/insight/src:services/orchestrator/src:services/analyzer/src:services/collector/src:libs/ws_common/src

python -m ml.cli build-dataset                     # 225 строк: 100 позитивов + 125 негативов
python -m ml.cli leak-check                        # контроль утечки разметки
python -m ml.cli train --embedder hashing          # обучение и экспорт артефакта без загрузки моделей
python -m ml.cli evaluate ml/data/raw/dataset_normalized.csv --embedder hashing

# тесты: 600 штук (565 исходных + 35 регрессионных тестов исправлений)
python -m unittest discover -s services/collector/tests/unit -t .
python -m unittest discover -s services/analyzer/tests/unit -t .
python -m unittest discover -s services/orchestrator/tests/unit -t .
python -m unittest discover -s services/orchestrator/tests/contract -t .
python -m unittest discover -s services/insight/tests/unit -t .
python -m unittest discover -s ml/tests -t ml

# статические проверки соответствия контрактам, схемам БД и требованиям к интерфейсу
python tools/check_proto_conformance.py
python tools/check_sql_conformance.py
python tools/check_ui_conformance.py
```

Интерфейс отдельно (нужен Node 22 и доступ к реестру npm):

```bash
cd services/ui && npm install && WS_API_BASE_URL=http://127.0.0.1:8080 npm run dev
```

---

## Если что-то пошло не так

| Симптом | Причина и что делать |
|---|---|
| `failed to compute cache key: "/uv.lock": not found` | сборка версии 1.2.0; в 1.2.1 исправлено — используйте этот архив и выполните `docker compose build --no-cache` |
| `pull access denied for weak-signals/...` | образа ещё нет в кеше: это нормально, сборка идёт следом. Ошибка исчезает после первой успешной сборки |
| `analyzer` не готов (`/readyz` 503), задания падают с `MODEL_NOT_LOADED` | в `model-store` нет активной модели: выполните шаг 3 (`trainer build-dataset` и `trainer train`); модель подхватится сама за 15 секунд |
| Все запросы API отвечают 500, в логах `comparing strings with non-ASCII characters` | в `.env` после значения стоит комментарий (`WS_API_KEY=   # …`): Compose читает его как значение. Комментарии — только отдельной строкой, см. `.env.example` |
| `password authentication failed` / `Role "ws_collector" does not exist` | том PostgreSQL создан без ролей: `make db-roles` (или `docker compose down -v` и повторный запуск, если данные не нужны) |
| Задание завершается статусом `PARTIAL` | нет ключей LLM либо часть источников ответила ошибкой; причины перечислены на странице результата |
| Интерфейс пишет «API недоступен» | не поднят `orchestrator-api`: `docker compose ps`, затем `docker compose logs orchestrator-api` |
| `401` в интерфейсе | в `.env` задан `WS_API_KEY`, но контейнер `ui` поднят со старым значением: `docker compose up -d ui` |
| `TS6133: ... is declared but its value is never read` при сборке `ui` | неиспользуемый импорт; найдите его командой `python tools/check_ui_conformance.py` и удалите |
| `TS2580: Cannot find name 'process'` | в `services/ui/package.json` нет `@types/node`; в версии 1.2.2 добавлен |
| Долгая первая сборка | образы python, node и модель e5 скачиваются один раз; повторные сборки берут кеш |

Полезные команды: `docker compose ps`, `docker compose logs -f orchestrator-worker`,
`docker compose build --no-cache <сервис>`, `docker compose down -v` (сброс данных).

## Структура репозитория

```
compose.yaml           состав системы: postgres, четыре сервиса, ui, trainer (профиль ml)
.env.example           все переменные окружения с пояснениями
proto/                 нормативные контракты gRPC (common, collector, analyzer, insight)
docs/api/              нормативный контракт HTTP API orchestrator (OpenAPI)
schemas/               реестр 25 признаков, схема манифеста модели, схема обучающей строки
libs/ws_common/        общая библиотека: конфигурация, логи, БД, миграции, метрики, интерсепторы
services/collector/    сбор документов из открытых источников
services/analyzer/     признаки, правила, модель, ранжирование, доказательства
services/insight/      нарративы и резюме источников через LLM с проверкой обоснованности
services/orchestrator/ HTTP API, очередь заданий, снимок результата
services/ui/           аналитическая панель (React 19 + TypeScript + Vite, раздача через nginx)
ml/                    конвейер обучения: данные, признаки, обучение, оценка, экспорт
tools/                 генерация стабов protobuf и статические чекеры
db/init/               создание ролей и схем PostgreSQL
```

## Документация

| Файл | Содержание |
|---|---|
| `docs/ml/evaluation_report.md` | фактические метрики модели, опорные решения, ограничения |
| `docs/ml/labeling_protocol.md` | протокол разметки отрицательного класса |
| `docs/*/DECISIONS.md` | журналы решений и противоречий по каждому компоненту |
| `docs/*/TEST_REPORT.md` | что проверено фактически и что не проверялось |
| `docs/api/orchestrator.openapi.yaml` | контракт HTTP API |
| `FILES.md` | состав поставки |

## Честно об ограничениях

- Отрицательный класс для обучения размечен командой; метрики измерены на собственной выборке
  и завышены — подробности и диагностика в `docs/ml/evaluation_report.md`.
- Каппа Коэна по перекрёстной разметке не вычислена (нужен второй аннотатор).
- Сборка фронтенда, интеграция с PostgreSQL, gRPC-вызовы и реальные вызовы LLM не проверялись в
  среде сборки: нет Docker, БД, доступа к реестру npm и ключей провайдеров. Тесты для всего
  этого написаны и входят в поставку; перечень непроверенного — в `docs/*/TEST_REPORT.md`.
