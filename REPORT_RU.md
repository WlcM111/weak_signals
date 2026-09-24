# Отчёт: подготовка эксперимента по улучшению LLM-части сервиса выявления слабых сигналов (weak-signals)

Дата подготовки: 22.09.2026. Исходный архив: `weak_signals__2_.zip` (sha256 `d1e4b030aea5c5b0fa84d89388d27e4f99031936ae92144b76a0f59efe51cd48`). Комплект: `experiments/llm-bench/` (новый каталог; существующие файлы проекта не изменены). Все команды и файлы — в `GUIDE_RU.md`; фактические проверки — в `checks/verification_log.md`.

Обозначения статусов: **PASS** — выполнено и проверено здесь; **NOT RUN** — не выполнялось (нет условий); **BLOCKED** — невозможно в среде подготовки (нет GPU/сети/БД); **CONFIRMED / INFERRED / NOT DISCLOSED** — статус внешних фактов.

---

## 1. Бизнес-требования, критерии успеха и матрица соответствия

**Блокер уровня документации.** В архиве нет ТЗ кейса, `ARCHITECTURE_TZ.md` и HANDOFF-файлов, на которые ссылаются `docs/*/DECISIONS.md` (§3.1, §6.3, §12.6, §20.1, §23). Матрица ниже собрана по косвенным источникам: `README.md`, `docs/ml/evaluation_report.md`, `docs/ml/labeling_protocol.md`, `services/insight/README.md`, `CHANGELOG_CONTRACTS.md`, `demo-*.txt`. Требования, которые я не смог подтвердить документом, помечены INFERRED — их надо сверить с ТЗ до финального отчёта хакатона.

| № | Требование (источник) | Статус источника | Как реализовано сейчас | Что проверяет комплект |
|---|---|---|---|---|
| R1 | Выдача ТОП-15 слабых сигналов по запросу (README, `requested_top_n` default 15) | CONFIRMED (код) | orchestrator + analyzer; UI | не меняется; оценивается только нарративный слой |
| R2 | Точность классификатора ≥ 80 % (приёмка 75 %) на 100 позитивах организаторов (evaluation_report) | INFERRED (нет ТЗ) | e5-LR: test acc 0.844, F1 0.829 на 45 строках (`ml/reports/runs.jsonl`, прогон 2026-09-20) | вне контура LLM; фиксируется как контекст (стилевая утечка, §4) |
| R3 | Для каждого сигнала — описание, преимущество, пример, объяснение, источники (ResultItem OpenAPI, схема `insight_llm_output.schema.json`) | CONFIRMED (код) | insight_v1 + GigaChat/YandexGPT/local llama.cpp; fallback экстрактивный | pipeline `v1_current`: те же поля, те же правила grounding |
| R4 | Обоснованность: числа и ссылки только из источников (insight grounding) | CONFIRMED (код) | `insight/domain/grounding.py` (жёсткие: числа, [doc:N], case_document_id; мягкие: ≥2 признака, кириллица ≥0.6) | метрики `hard_violation`, `unsupported_numbers`, `critical` |
| R5 | Русский язык выдачи (промпт: «только по-русски») | CONFIRMED (промпт) | доля кириллицы ≥ 0.6 — единственная проверка | эвристики языка (§5, §16) + слепая экспертная оценка |
| R6 | Бизнес-полезность: что делать с сигналом, риски, как проверить (ТЗ промпта пользователя) | требование эксперимента | отсутствует в контракте v1 | контракт v2 `schemas/card_v2.schema.json` (§11) |
| R7 | Локальное развёртывание LLM возможно (compose-профиль `local-llm`, провайдер `local_llamacpp`) | CONFIRMED (compose, config) | GGUF `yandexgpt-5-lite-8b-instruct-q4_k_m.gguf`, ctx 8192, CPU | кандидаты подключаются тем же OpenAI-совместимым интерфейсом |
| R8 | Время ответа narrate в бюджете job (job_stats.narrate_ms) | INFERRED | бюджет числом в архиве не найден | `latency_p50/p95` фиксируются; порог — H9 (60 с на кандидата, допущение) |

Критерии успеха эксперимента заданы не «победителем», а gate-правилами (§22): прирост `business_ready` с доверительным интервалом, отсутствие роста критических нарушений, экспертная оценка, устойчивость к инъекциям и недостатку данных, задержка в бюджете.

## 2. Реестр изученных материалов

Полный реестр — `registry_files/registry.md` (455 файлов без кешей: 328 в `services/`, 45 в `ml/`, 36 в `libs/`, 17 в `docs/`). Прочитаны полностью 72 файла (все модули insight, ключевые модули analyzer/orchestrator/collector, все DECISIONS/TEST_REPORT, данные ML, промпты, схемы, compose, demo-выдачи), частично — 12, тесты всех сервисов выполнены (PASS, см. §4), DDL миграций изучены через grep. Файлы со статусом «перечислен» на выводы не влияли.

## 3. Схема текущего проекта и ML-части (as-is)

Стек: Python 3.12 (uv/pyproject), gRPC между сервисами, PostgreSQL 18 (порт 5599, БД `weaksignals`, схемы `orchestrator/collector/analyzer`), React/TS UI, Docker Compose. Go в архиве нет (расхождение с формулировкой запроса).

Конвейер job: `collect` (адаптеры источников: arXiv, GitHub, RSS/новости, вакансии и др.; доверие по `trust_rules.yaml`) → `analyze` (e5-эмбеддинги, кластеризация, 24 признака, LR elastic-net, правила исключения MATURE/HYPE/INSUFFICIENT/OFF_TOPIC, стадия/тренд) → `narrate` (insight: до 8 evidence × 2000 символов, промпт `insight_v1.j2`, провайдер, grounding, 2 попытки, затем `FALLBACK_EXTRACTIVE`) → запись `orchestrator.result_items` (+ `result_item_sources`, `result_item_features`) → UI.

Два ML-контура:

1. **Классификатор** (analyzer/ml): обучается `ml.cli train` на 225 строках (`dataset_ds-2026.09.19-v2.jsonl`), артефакт `releases/wsclf-2026.09.16-2.*`. Не предмет этого эксперимента.
2. **Нарративная LLM** (insight): облачная (GigaChat-2-Pro / YandexGPT) или локальная (`WS_LLM_LOCAL_ENABLED`, `WS_LOCAL_LLM_BASE_URL`, провайдер `local_llamacpp` допустим как `WS_LLM_PRIMARY_PROVIDER`). Дообучения нет, обучающих данных нет, оценки качества генерации нет (тесты — на `FakeProvider`). **Именно этот контур — предмет эксперимента.**

Поток данных LLM: `CandidateContext` (title_auto из n-грамм заголовков, keyphrases, score, decision analyzer, top-8 признаков с label_ru) + `EvidenceDocument[]` → JSON-пользовательское сообщение → ответ по схеме из 6 полей → grounding → Narrative. Тексты промптов/ответов в БД не хранятся; хранятся итоговые тексты и `narrative_status`, `llm_provider`, `llm_model`, `prompt_version`.

## 4. Аудит ML и смежных модулей: дефекты и наблюдения

Тесты проекта в среде подготовки: insight 102, analyzer 198, ml 49, collector 152, orchestrator 87+28 — все PASS; `tools/check_proto_conformance.py`, `check_sql_conformance.py`, `check_ui_conformance.py` — PASS (`docs/*/TEST_REPORT.md` называют меньшее число тестов — документация отстаёт).

| ID | Дефект/наблюдение | Файл(ы) | Воспроизведение | Влияние | Кто чинит |
|---|---|---|---|---|---|
| D1 | Несовпадение адреса локальной LLM: default `local_llm_base_url = http://localhost:8090` в `services/insight/src/insight/config.py`, в `compose.yaml` сервис `local-llm:8000` | config.py, compose.yaml | `grep -n local_llm_base_url services/insight/src/insight/config.py; grep -n 8000 compose.yaml` | при запуске вне compose без явного env insight не найдёт сервер | продукт (не в этом комплекте) |
| D2 | `insight/domain/endpoint_policy.py` нигде не используется (мёртвый код) | endpoint_policy.py | `grep -rn endpoint_policy services/insight/src --include=*.py` → только определение | путаница при чтении; политика выбора endpoint фактически в `provider_chain.py` | продукт |
| D3 | Промпт `insight_v1.j2` требует «ровно семь ключей», а перечислено шесть; схема — 6 полей | prompts/insight_v1.j2 | прочитать правило форматирования | модель может добавить лишний ключ → отклонение по `additionalProperties` | продукт; в v2 промпте исправлено |
| D4 | Нет данных и кода для дообучения/оценки LLM; качество нарратива не измеряется | services/insight | `ls services/insight` — нет eval/train | невозможно сравнивать провайдеров количественно | комплект (стенд) |
| D5 | Стилевая утечка в классификаторе: негативы написаны командой нейтрально, позитивы — методологами; TF-IDF baseline F1 0.859 ≈ e5 0.829 (`docs/ml/evaluation_report.md` §7a) | ml/ | `ml/reports/style_diagnostic.json` | завышенная accuracy; каппа разметчиков не считалась | вне контура LLM; учитывается как ограничение gold C1 |
| D6 | `ml/leak_patterns.yaml` regex-очистка ломает грамматику описаний (обрывки фраз) | ml/dataset.py | `python -c` с `clean_description` на строке 1 датасета | только классификатор; в C1 задача T2 наследует ломаный текст (помечено `cleaning_applied`) | вне LLM |
| D7 | В demo-выдачах: скоринг ~99 % у нерелевантных кластеров; PARTIAL-статусы сбора; сгенерировано ≤ 8 нарративов из 17 (остальные fallback) | demo-20260921-0442.txt, demo-rel-20260921-1636.txt | открыть файлы | baseline A частично экстрактивный — важно для честного сравнения (учитывается `prod_generated_share`) | продукт |
| D8 | Языковые дефекты L1–L7 (§5) | см. §5 | см. §5 | часть дефектов не в LLM: дообучение их не исправит | см. §5 |
| D9 | Не хранится текст промпта/ответа LLM (по ТЗ §14.5) → baseline A восстанавливается только по итоговым текстам | orchestrator DDL | `result_items` без raw-полей | сравнение A на C2 возможно по текстам, но без токенов/задержки | стенд: `import-prod-baseline` |
| D10 | Провайдер `local_llamacpp` не имеет отдельного теста реального сервера; `enable_thinking` для Qwen3-моделей не передаётся | openai_compat_provider.py | прочитать `complete()` | гибридные модели могут отвечать в режиме thinking → невалидный JSON | стенд: `extra_body.chat_template_kwargs`; для прода — см. §22 |

## 5. Причины ошибок русского языка (что LLM, что нет)

| ID | Симптом (из demo) | Причина | Файл | Воспроизведение | Исправляется дообучением LLM? |
|---|---|---|---|---|---|
| L1 | «нового бизнеса», «современной медицине», «ИИ превратит интернет» как названия сигналов | `title_auto` = n-грамма заголовков в исходном падеже | `analyzer/domain/keyphrases.py` | демо, поле title_auto | **Нет** — это analyzer. LLM в v1 переписывает `title_ru`, но fallback [э] берёт title_auto как есть (L2) |
| L2 | Названия в косвенном падеже в fallback-карточках | fallback пропускает title из L1 | `insight/application/fallback.py`, `orchestrator/.../narrate.py` | демо, статус FALLBACK_EXTRACTIVE | **Нет**, но частота fallback падает, если модель проходит grounding с первой попытки (метрика `v1_first_pass_accept`) |
| L3 | «доля … равен 0.5» (нет согласования рода) | правило 7 промпта: шаблон «<label_ru> равен <value>» | `prompts/insight_v1.j2` | прочитать правило 7 | Частично: модель может согласовывать; в v2 шаблон не навязывается |
| L4 | Лишние/отсутствующие ключи | «ровно семь ключей» при шести (D3) | insight_v1.j2 | — | Нет — промпт |
| L5 | Механические фразы «label = value (direction)» | `fallback.py`, `rules.py` | — | демо [э] | Нет — fallback |
| L6 | Смешение языков, обрезка текста [:200] | narrate.py усечение summaries, английские заголовки источников | `orchestrator/.../narrate.py` | демо [M] | Частично: смешение — LLM, обрезка — orchestrator |
| L7 | Ломаная грамматика описаний в датасете | regex-очистка (D6) | `ml/leak_patterns.yaml` | — | Нет — данные классификатора |

Вывод для дизайна эксперимента: **измеряемые дообучением** цели — согласование и естественность текста, доля первой приёмки (меньше fallback), корректная работа с русскими/английскими источниками, отсутствие смешения языков. L1/L2/L4/L5/L7 требуют правок продукта, они зафиксированы как дефекты и не входят в критерии успеха LLM.

## 6. Обзор сопоставимых сервисов и подходов

Проверено поиском 21–22.09.2026 (CONFIRMED = есть открытая публичная страница/публикация; INFERRED = по описанию; NOT DISCLOSED = данных нет).

| Сервис / подход | Что делает | Как формирует «сигналы» | LLM-нарратив / бизнес-выводы | Статус |
|---|---|---|---|---|
| ITONICS (Signals, Trend Radar, Scouting) | платформа forecasting/foresight; сбор сигналов, тренд-радар | ручная + автоматизированная курация, ИИ-помощник | ИИ-суммаризация сигналов; выводы — аналитик | CONFIRMED (сайт), детали моделей NOT DISCLOSED |
| FIBRES / Valona | сбор сигналов, кластеризация в тренды, «AI insights» | автоматическая группировка сигналов, ежедневные обновления | генеративные сводки | CONFIRMED (сайт) |
| Newness | AI-мониторинг «слабых сигналов» в новостях/патентах/финансировании | классификация + верификация | генеративные объяснения | CONFIRMED (сайт), метод NOT DISCLOSED |
| Futures Platform (Synapse) | база трендов + weak signals, AI-агент | AI-агент выделяет weak signals | текстовые обоснования | CONFIRMED (сайт) |
| WEF Strategic Intelligence | карты трансформаций, слабые сигналы | экспертная курация + ИИ | сводки | CONFIRMED |
| S2T GoldenSpear | CTI/OSINT, «weak signals» | модели на потоках данных | — | CONFIRMED (сайт), NOT DISCLOSED |
| KISTI (Корея) — automated weak signal detection | публичный подход: рост частоты слабых терминов в текстах (по Yoon 2012) | keyword emergence map + LLM-фильтр | нет бизнес-карточки | CONFIRMED (публикации) |
| Envisioning, Horizon Scan AI, Amplyfi, Mergeflow, Trendtracker, Shaping Tomorrow, CB Insights, Quid | тренд-/tech-scouting платформы | графы, NLP, ИИ-ассистенты | сводки и радары | INFERRED (общие знания; сайты в этой сессии не открывались) |
| Академические методы: Ansoff (1975), Hiltunen «future sign», Yoon (2012) keyword emergence map, Mühlroth & Grottke (2018/2020), Ebadi et al. (2022), Roßmann et al. (2020) | определения и метрики слабого сигнала (DoV/DoD — частота и рост) | статистика терминов, кластеризация, эмбеддинги | нарратив — человек | CONFIRMED (публикации; год из памяти) |
| Работы 2024–2026 по LLM для horizon scanning (эмбеддинги + LLM-разметка «сигнал/шум/зрелый», объяснения с цитированием) | ближайший аналог нашей задачи | LLM как классификатор+объяснитель с grounding | да | CONFIRMED как направление; конкретные метрики NOT DISCLOSED |

Что важно для нашего дизайна: (1) ни один сервис не публикует эталонных данных «сигнал/не сигнал» — сравнение с ними невозможно, только с собственным baseline; (2) общий приём — разделение «сигнал / зрелый / хайп / недостаточно данных» с обязательным цитированием источников, это и заложено в контракт v2; (3) LLM используется как объяснитель поверх статистических признаков, а не как единственный классификатор — совпадает с архитектурой проекта.

## 7. Кандидаты локальных моделей: широкий список → n

Проверено 21.09.2026 по карточкам Hugging Face / релизам (CONFIRMED), остальное — CHECK.

| Модель (HF id) | Размер | База/архитектура | Русский | Лицензия | GGUF | Дообучение LoRA (PEFT) | Решение |
|---|---|---|---|---|---|---|---|
| `t-tech/T-lite-it-2.1` | 8B dense | Qwen3-8B, non-thinking, 32k | специализирована (Ru Arena Hard 83.9 vs 57.2 у Qwen3-8B) | Apache-2.0 CONFIRMED | есть | стандартная | **основной** |
| `yandex/YandexGPT-5-Lite-8B-instruct` | 8B dense | Llama-архитектура, 32k | специализирована | собственная «yandexgpt-5-lite-8b» — условия коммерческого использования **CHECK** | есть (стоит по умолчанию в compose проекта) | стандартная | **основной** (при допустимой лицензии) |
| `Qwen/Qwen3-8B` | 8B dense | Qwen3, гибридный thinking | общая многоязычная | Apache-2.0 CONFIRMED | есть | стандартная; `enable_thinking=false` | **контроль** (та же база, что T-lite) |
| `ai-sage/GigaChat3.1-10B-A1.8B-bf16` | MoE 10B / 1.8B акт. | MLA, 256k, custom code | специализирована | MIT CONFIRMED | есть | **риск**: кастомная архитектура; включать только после `train --preflight` | опциональный |
| `t-tech/T-pro-it-2.1` | 32B dense | Qwen3-32B | специализирована | CHECK | есть | QLoRA только при VRAM ≥ 40 ГБ | потолок (inference-only) |
| `Qwen/Qwen3.5-9B`, `Qwen3.6-27B`, `Qwen3.8-27B` | 9B / 27B | новые поколения Qwen (2026) | общая | лицензии по моделям CHECK (Qwen3.8-27B — Apache-2.0 по анонсу) | частично | стандартная | резерв; не включены без проверки лицензии/русского |
| `google/gemma-3-12b-it` | 12B | Gemma | общая | Gemma Terms (не Apache) | есть | стандартная | исключена: условия лицензии |
| `meta-llama/Llama-3.1-8B-Instruct` | 8B | Llama | слабее в русском | Llama Community | есть | стандартная | исключена |
| `mistralai/Ministral-3-8B-Instruct-2512` | 8B | Mistral | общая | CHECK | ? | стандартная | исключена: лицензия/русский не проверены |
| RuadaptQwen3-8B-Hybrid, Vikhr-*, Saiga-* | 7–12B | адаптации | русские | разные | частично | стандартная | исключены: меньше подтверждений качества/поддержки, чем у T-lite/YandexGPT |
| `openai/gpt-oss-20b` | MoE 21B/3.6B | — | русский не проверен | Apache-2.0 | есть | нестандартный формат | исключена |
| Cotype-Nano (1.5B) | 1.5B | — | русская | — | — | — | исключена: мала для контракта v2 |

**n = 3 (предварительно): T-lite-it-2.1, YandexGPT-5-Lite-8B-instruct, Qwen3-8B (контроль).** Обоснование: (а) 8B — единственный класс, который за 8 дней на одном GPU 12–24 ГБ можно и обучить (QLoRA), и прогнать по всем split-ам; (б) две русскоязычные специализации разной архитектуры + их общая база-контроль дают ответ на H2; (в) GigaChat3.1 добавляется четвёртым только при прохождении preflight обучения (иначе — inference-only сравнение); (г) 32B — только как ориентир на XL-железе. **n окончательно фиксируется после `hw-preflight` (§8) и `model-preflight`** (лицензия YandexGPT — прочитать LICENSE; если коммерческое использование ограничено, заменить на Qwen3.5-9B после проверки лицензии).

Не выяснено: точные commit sha (фиксирует `model-preflight` в `config/candidates.lock.json`); имена GGUF-репозиториев T-lite 2.1 (проверить); реальная поддержка `chat_template_kwargs` в версии llama.cpp пользователя.

## 8. Аппаратная диагностика и оценка памяти

Железо пользователя **не известно** и не подставлялось. Команда `python -m bench hw-preflight` (Linux/WSL2/macOS/Windows) пишет `config/hardware.json`: ОС, CPU, RAM, GPU (nvidia-smi / rocm-smi / Apple Silicon), диск, Docker и GPU-runtime, версии torch/transformers/peft/bitsandbytes, и уровень:

| Уровень | VRAM | Что возможно |
|---|---|---|
| CPU | нет GPU | только inference GGUF (медленно); дообучение локально невыполнимо → арендованный GPU |
| S | 6–11 ГБ | inference Q4 8B частично на GPU; обучение нет |
| M | 11–22 ГБ | QLoRA 4-bit 8B, ctx ≤ 4–6k, micro_batch 1 |
| L | 22–40 ГБ | LoRA bf16 8B (ctx ≤ 6k) или QLoRA 8B/14B |
| XL | ≥ 40 ГБ | LoRA 8B и QLoRA 27–32B |
| APPLE | unified memory | inference llama.cpp/MLX; LoRA только MLX-LM (bitsandbytes нет) |

Формулы (оценки, не измерения; реализованы в `hw_preflight.memory_estimates`): веса bf16 ≈ 2·P ГБ; Q4/NF4 ≈ 0.56–0.6·P ГБ; KV-кэш на токен = 2·L·H_kv·d·2 байт (bf16); QLoRA ≈ 0.6·P + 2·KV(ctx) + 3 ГБ; LoRA bf16 ≈ 2·P + 2·KV(ctx) + 3 ГБ. Для 8B при ctx 8192: KV ≈ 1.1 ГБ (36 слоёв, 8 KV-голов, d=128), QLoRA ≈ 10 ГБ, LoRA bf16 ≈ 22 ГБ. Проверяется фактически в `train --preflight` (peak VRAM пишется в реестр).

## 9. Реестр гипотез

См. `protocol/protocol_v1.yaml → hypotheses` (H1–H9): для каждой — формулировка, метрика реестра, правило решения. Кратко: H1 локальная 8B без дообучения не хуже облака по приёмке v1; H2 русская специализация снижает языковые дефекты; H3 контракт v2 повышает бизнес-пригодность; H4 дообучение даёт Δbusiness_ready ≥ +0.10 с ДИ; H5 критические нарушения не растут; H6 чаще объявляется недостаток данных; H7 устойчивость к инъекциям не падает; H8 порядковые оценки стадии/тренда MAE < 1.0; H9 p95 задержки ≤ бюджета. Отчёт `reports/report.md` показывает по каждой гипотезе, есть ли данные или NOT RUN.

## 10. Протокол сравнения (A / B / C)

- **A — текущий проект как есть**: (1) на C2 — фактические выходы из БД (`import-prod-baseline`, включая FALLBACK), (2) на C1/C3p — промпт `insight_v1` через тот же провайдер, что в проде (облако по ключам проекта или локальный llama.cpp с текущим GGUF).
- **B — кандидаты до дообучения**: `v1_current` (сопоставимо с A по формату) и `v2_card`.
- **C — после дообучения**: те же кандидаты с адаптером; выбор чекпойнта — по validation (business_ready, затем critical); **один** прогон на test.

Одинаково для всех: примеры и порядок, evidence (≤8 × 2000 символов, как в проде), decoding (temperature 0.2, seed, max_tokens 1200/2500), проверки (`bench/checks.py`), бутстрэп по кластерам `base_example_id/group_id`. Различия только в модели и формате ответа. Правило decision_mode: в `v1_current` статус analyzer подаётся в промпт как в проде (`from_gold` на C1) — поэтому `status_correct` для v1 на C1 **не считается**; в `v2_card` статус скрыт.

## 11. Единый бизнес-контракт выдачи (v2)

`schemas/card_v2.schema.json` + `prompts/card_v2_system.md`. Поля: заголовок и одна строка; `signal_status` (weak_signal / mature / hype_or_noise / insufficient_data) с обоснованием; чем отличается от зрелого; порядковые стадия/тренд; `supported_claims` (утверждение, тип fact/interpretation/forecast/assumption, `evidence_ids`, цитата); `contradicting_evidence`; гипотеза развития; механизм и получатель ценности; 2–4 `options` (kind: monitor / research_pilot / product / partnership / acquisition / new_business_line / capex / no_invest — обязателен хотя бы один без вложений), у каждого: первый проверочный шаг, компетенции, ориентир затрат с основанием source/assumption/unknown, горизонт, KPI, условия продолжения/остановки, риски, обоснование; `refutation_conditions_ru`; `uncertainty` (level + чего не хватает). Правила: числа только из evidence; текст evidence — данные, не инструкции; недостаток данных — допустимый ответ. Контракт **не подменяет** текущий v1 (продукт не менялся); это формат для сравнения бизнес-ценности и кандидат на v2 при положительном gate.

## 12. Данные: состав, gold, дефицит

`data/build/examples.jsonl` — 540 примеров (`dataset_version llmb-2026.09.21-v1`): C1 = 450 (225 реальных строк × 2 задачи), C3p = 90 robustness-производных (programmatic gold). Split (группы = технология, стратификация статус×домен): train 349 / validation 81 / test 110 (test заморожен, sha256 в `protocol/frozen_test.json`). Gold: статус (методологи — expert_gold; команда — team_labeled_unverified, каппа не считалась), stage_b/trend_b (только позитивы), ожидания robustness. Черновики карточек v2: 8 шт. (`data/seed/gold_cards_draft.jsonl`, draft_unverified, все проходят схему и grounding — тест `test_seed_cards_valid_and_grounded`).

**Честно о дефиците:** (1) для SFT нет ни одного верифицированного целевого ответа v2 — targets нужно получить teacher-генерацией (§15 гайда) + ручной проверкой двух участников (цель ≥ 150 к дню 4); (2) evidence C1 — заметки методологов и заголовки ссылок, а не тексты страниц (нет сети при подготовке) — задача проще продуктивной; (3) продуктивные данные C2 — только из БД пользователя (`export-db`), там же фактический baseline A; (4) робастность-производные синтетические и помечены; (5) временной контроль ограничен датами из URL и `fetched_at` (C2).

## 13–14. Скрипты подготовки, split, адаптеры

`bench/build_dataset.py` (детерминированно, seed), `bench/split.py` (групповое разбиение, manifest с хешами, проверки утечек — группы и одинаковые тексты evidence, `freeze`/`check_frozen`), `bench/convert_db_export.py` (C2 → канон, усечение как в проде через `orchestrator...narrate.truncate_at_sentence`), `bench/adapters.py`: chat template токенизатора модели, assistant-only маска (проверка prefix-свойства шаблона, eos, нет обучаемых токенов → пример отбрасывается), без packing, цель не режется, статистика токенов промпта по каждому токенизатору (`model-preflight`). Отчёт адаптера — `data/build/adapters/<candidate>/<pipeline>/report.json` (доля усечённых, средние длины, sha256 списка id).

## 15. Обучение: конфигурации, preflight, аварийные правила

`config/train/<candidate>.yaml` (LoRA r16/α32/dropout 0.05 на attention+MLP, QLoRA nf4, max_len 8192, micro_batch 1 × grad_accum 8, lr 1e-4, 2 эпохи, cosine, warmup 5 %, eval/save каждые 25 шагов, seed). `python -m bench train --candidate X --preflight` проверяет 10 пунктов: (1) загрузка модели/токенизатора с ревизией; (2) LoRA-модули найдены, доля обучаемых параметров; (3) данные адаптера: prefix шаблона, маска, доля усечённых; (4) forward/backward 2 шага, loss конечен; (5) градиенты есть; (6) тензоры адаптера изменились; (7) сохранение адаптера; (8) повторная загрузка; (9) inference с адаптером; (10) peak VRAM и время — всё в реестр. Аварийные правила: NaN/Inf loss → стоп; OOM → статус `oom` с рекомендацией (micro_batch/max_len/4bit), новая попытка = новая attempt; отсутствие targets → отказ до старта. `verify-checkpoint` сравнивает `adapter_init` и `adapter` по тензорам, проверяет sha256 из реестра, генерирует с адаптером и без.

## 16. Исполнимый стенд

`python -m bench <команда>`: check-env, build-data, split, freeze, validate-data, smoke, hw-preflight, model-preflight, baseline/eval, train, verify-checkpoint, export-db, convert-db, import-prod-baseline, teacher, human pack/ingest/agreement, report, run-all. Бэкенды: `openai_compat` (llama.cpp server / vLLM / Ollama / YandexGPT), `hf_local` (transformers + адаптер), `gigachat_sdk`, `mock` (только тесты; результаты помечены is_mock=1). Метрики (§ protocol): business_ready, critical и hard_violation отдельно, unsupported_numbers, status_correct и macro-F1, stage/trend MAE, robustness-ожидания, options_ok, языковые эвристики (+ pymorphy3 при наличии), v1_first_pass_accept, задержка p50/p95, токены. Возобновление: повторный запуск с `--resume <run_id>` не повторяет готовые примеры. 25 unit-тестов стенда PASS (mock end-to-end, утечки, маски, реестр, бутстрэп).

## 17. Единый реестр результатов

`registry/registry.sqlite` (WAL + файловая блокировка): таблицы runs (run_id = `<exp>__<candidate>__<variant>__<pipeline>__<split>__a<attempt>`, хеши кода/протокола/промпта/split/адаптера, снимок среды, конфиг), predictions (сырой ответ, разбор, проверки, задержка, токены), metrics (значения, n, ДИ), events, artifacts, candidates, human_scores. Экспорт: `reports/{runs,summary,per_example,errors}.csv`, `report.md`, `report.html` (`python -m bench report --baseline <run_id>` добавляет парный бутстрэп). NOT RUN — `None`, не ноль; невалидные ответы в знаменателе.

## 18. Копипаст-гайд

`GUIDE_RU.md`: шаги 0–16, для каждого — цель, предусловия, где выполнять, команды (Linux/NVIDIA, WSL2, macOS), ожидаемый вывод, проверка, действия при ошибке, время, артефакты, запись в реестр, следующий шаг. Существующие файлы проекта не правятся; интеграция в prod-контур для arm A с локальной моделью — только через переменные окружения (`WS_LLM_PRIMARY_PROVIDER=local_llamacpp`, `WS_LOCAL_LLM_BASE_URL`), см. шаг 6.

## 19. План на 8 дней (2 человека)

`plan/8day_plan.md`: критический путь — teacher-targets и ручная проверка (дни 2–4), обучение (дни 4–6), test-прогоны (день 7), отчёт (день 8); машинное и активное время разделены; правила остановки при срыве.

## 20. Отчёт о проверках

`checks/verification_log.md`: 14 команд с кодами возврата (PASS / ожидаемый отказ), хеши кода и данных; список NOT RUN/BLOCKED с причинами (нет сети, GPU, БД, оценщиков).

## 21. Артефакты к возврату после прогонов

1. `config/hardware.json`, `config/candidates.lock.json`; 2. `registry/registry.sqlite` + `reports/*`; 3. `runs/<run_id>/` (predictions.jsonl, metrics.json, learning_curve.json, train_report.json, verify_checkpoint.json, адаптеры `adapter/` — веса по договорённости); 4. `data/build/` (examples.jsonl, splits, adapters/*/report.json, teacher/*, db_export manifest — без body_text, если данные нельзя передавать); 5. `human_eval/packets/*/scores*.csv` + agreement; 6. `protocol/frozen_test.json`; 7. заполненный `checks/verification_log.md` с их среды; 8. логи серверов моделей (версии llama.cpp/vLLM, флаги запуска).

## 22. Условия gate-решения и что осталось невыясненным

Gate (см. `protocol_v1.yaml → decision_gate`): переход к интеграции только при Δbusiness_ready ≥ +0.10 на test с ДИ, не включающим 0, при n ≥ 60; critical ≤ baseline и ≤ 0.05; экспертная оценка ≥ 2.5/3 по обоснованности и языку при каппе ≥ 0.4; injection ≥ 0.95, insufficient ≥ 0.8; p95 в бюджете. Остановка: рост critical после дообучения; двукратный провал preflight; < 150 верифицированных targets к дню 4 (тогда выводы только по B).

Не выяснено и требует ответа пользователя: (1) железо; (2) ТЗ и HANDOFF (матрица требований INFERRED); (3) какой провайдер/модель реально работали в demo (`llm_provider/llm_model` в БД) и какой GGUF запускался локально; (4) условия лицензии YandexGPT-5-Lite для коммерческого использования; (5) бюджет времени narrate; (6) объём и доступность БД для C2; (7) можно ли передавать тексты источников третьим лицам (оценщикам).

Комплект **не называет победителя**: любые выводы — только по числам реестра после прогонов на железе пользователя.
