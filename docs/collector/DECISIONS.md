# Решения и расхождения источников (компонент collector)

Зафиксированы противоречия между нормативными материалами и принятые решения. Публичное поведение
контрактов (`.proto`, DDL) не менялось.

| ID | Противоречие / вопрос | Источники | Решение |
|---|---|---|---|
| C-1 | Итоговый статус при исчерпании бюджета времени: HANDOFF §7 перечисляет только «все COMPLETED → COMPLETED / ≥1 FAILED → PARTIAL / все FAILED или 0 документов → FAILED» и называет `BUDGET_EXHAUSTED` неошибкой; ARCHITECTURE §9.2 требует PARTIAL при исчерпании бюджета | `handoffs/collector/HANDOFF_COLLECTOR.md` §7, `ARCHITECTURE_TZ.md` §9.2 | Запуск адаптера, прерванный дедлайном, получает статус COMPLETED с пометкой `error_code=BUDGET_EXHAUSTED` (не отказ); коллекция при этом получает PARTIAL. Достижение `max_total_documents` — штатное завершение (COMPLETED без пометки), так как запрошенный объём собран |
| C-2 | `wikipedia` включён в `source_catalog` (`enabled=true`), но §6.5 определяет его только как индикатор зрелости | `appendix/C_sql/collector/0001_init.sql`, `ARCHITECTURE_TZ.md` §6.5 | Адаптер не участвует в сборе: в набор «все включённые адаптеры» не входит. При явном запросе `SOURCE_KEY_WIKIPEDIA` в `sources` запуск помечается `FAILED/DISABLED` с сообщением «используется только через CheckEncyclopedia» — данные не выдумываются |
| C-3 | ARCHITECTURE §9.2 упоминает правило `http→https для известных хостов` в `canonical_url`, список хостов не задан; HANDOFF §6 такого правила не содержит | `ARCHITECTURE_TZ.md` §9.2, HANDOFF §6 | Схема URL не переписывается: без нормативного списка хостов замена схемы может дать неработающий URL и разойтись с `url_hash` соседних сервисов. Остальные правила канонизации реализованы дословно |
| C-4 | Порог релевантности записи RSS: HANDOFF §8 — «доля токенов термина ≥ 0.5», ARCHITECTURE §9.2 — «Jaccard ≥ 0.2» | HANDOFF §8, `ARCHITECTURE_TZ.md` §9.2 | Реализован порог HANDOFF (покрытие токенов фразы ≥ 0.5) как более специфичное задание компонента; значение вынесено константой `RELEVANCE_THRESHOLD` |
| C-5 | Каталог генерации стабов: ARCHITECTURE §15.5 — `--python_out=libs/ws_contracts/src`, `scaffold/create_project_structure.sh` — `libs/ws_contracts/src/ws_contracts` | `ARCHITECTURE_TZ.md` §15.5, `scaffold/create_project_structure.sh` | Выбран вариант §15.5: только он даёт работающие внутренние импорты сгенерированных модулей (`from weaksignals.common.v1 import common_pb2`). `tools/gen_proto.sh` и Dockerfile приведены к этому варианту |
| D-1 | `feedparser` для лент RSS (HANDOFF §8) | HANDOFF §8 | Разбор лент выполняется тем же безопасным парсером `lxml`, что и Atom arXiv (`resolve_entities=False`, `no_network`): единая защита от XXE и на одну зависимость меньше. Поддержаны RSS 2.0 и Atom, поля соответствуют требуемым (`published_parsed` → `published_at`). Замена локализована в `sources/rss.py` |
| D-2 | `langdetect` как определитель языка (HANDOFF §6) | HANDOFF §6 | `langdetect` остаётся рабочей зависимостью и используется при длине текста ≥ 20 символов; при его отсутствии или неуверенном ответе применяется детерминированный резерв по преобладающему алфавиту, код языка приводится к ISO 639-1, иначе `und` |
| D-3 | Неожиданное исключение адаптера | §10.7 (закрытый список кодов) | Отображается на `PARSE_ERROR` — в едином списке нет кода для внутренней ошибки адаптера; сообщение содержит текст исключения, сбор продолжается остальными адаптерами |
| D-4 | HTTP-эндпоинты обслуживания | §9 COMMON | Реализованы на asyncio без веб-фреймворка: три статических маршрута не оправдывают зависимость уровня FastAPI в gRPC-сервисе |
| D-5 | Логирование в прикладном слое | §9 COMMON (structlog) | `ws_common.logging` — фасад: structlog при наличии, иначе stdlib logging с тем же составом полей. Состав событий и маскирование секретов не меняются; фасад позволяет проверять домен и use cases без тяжёлых зависимостей |

## Новые модули вне перечня `scaffold/create_project_structure.sh`

| Файл | Причина |
|---|---|
| `application/validation.py` | Правила §10.1 вынесены из gRPC-слоя, чтобы проверяться без транспорта и переиспользоваться use cases |
| `domain/normalization.py` | Сборка `RawDocument → DocumentDraft`: отдельный модуль исключает цикл импортов `rules ← entities` |
| `adapters/outbound/rules_loader.py` | Чтение `config/*.yaml` в доменную конфигурацию (домен без файлового ввода-вывода) |
| `adapters/outbound/sources/xml_utils.py` | Единая безопасная точка разбора XML для arXiv и RSS |
| `adapters/outbound/postgres/{mappers,source_catalog}.py` | Отображение строк БД в домен и чтение каталога источников при старте |
| `adapters/outbound/metrics.py` | Реализация порта `MetricsSink` на prometheus_client (домен не зависит от экспортера) |
| `adapters/inbound/worker.py` | Драйвер очереди коллекций и фонового обслуживания |
| `config_parsing.py` | Разбор составных переменных окружения без pydantic (проверяется модульными тестами) |
| `application/use_cases/purge_documents.py` | Ретенция (§11.9 ТЗ, §7 HANDOFF) |

## 2026-09-27 — базовый образ collector на Debian 12 (bookworm) из-за отказов CDN arXiv

**Факт.** В образе `python:3.12-slim` (Debian 13 trixie, Python 3.12.14, OpenSSL 3.5.7) каждый запрос к
`export.arxiv.org` без ответа в кеше CDN получал 406 с пустым телом. В `via` были только узлы Varnish без
`google`, стоял `cache-control: private, no-store`, путь в `x-served-by` обрывался на узле LGA — до серверов
arXiv запрос не доходил. Матрица 26.09.2026 (один IP, одна сеть Docker, одинаковые запросы с паузой 8 с):

| Образ | Python | OpenSSL | Результат |
|---|---|---|---|
| `weak-signals/collector:1.0.0` (slim, trixie) | 3.12.14 | 3.5.7 | 406, 406 |
| `python:3.12-slim-trixie` | 3.12.14 | 3.5.7 | 406, 406 |
| `python:3.12-slim-bookworm` | 3.12.14 | 3.0.20 | 200, 200 |

**Решение.** Базовый образ collector — `python:3.12-slim-bookworm` (OpenSSL 3.0). Остальные сервисы с arXiv не
работают и не меняются.

**Как не сломать снова.**
- Переход collector на trixie (или новый тег `slim`) — только после повторной проверки матрицей образов.
- При старте collector пишет в журнал `openssl` — смена TLS-библиотеки после обновления образа видна сразу.
- Отказ HTTP сохраняет заголовки `via`, `x-cache`, `x-served-by`. 406 без узла `google` в `via` помечается
  текстом «CDN arXiv отклонил TLS-клиент, запрос не дошёл до сервера» (код HTTP_4XX — контракт не меняется).
- Для arXiv — отдельный HTTP-клиент без внутренних повторов: шлюз частоты и 30-минутная пауза адаптера
  закрывают все отказы, а повторы 429/5xx в обход шлюза нарушали бы правило arXiv о частоте.
- Статьи arXiv продолжают приходить и через OpenAlex, если API arXiv снова откажет.
