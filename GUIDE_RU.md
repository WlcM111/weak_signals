# Копипаст-гайд llm-bench (шаги 0–16)

Формат каждого шага: **Цель · Предусловия · Где · Команды · Ожидаемый вывод · Проверка · Если не получилось · Время · Артефакты · Запись в реестр · Дальше.** Все команды выполняются из корня репозитория `weak-signals` (каталог, где лежит `compose.yaml`), если не сказано иное. `<...>` — подставить своё значение. Существующие файлы проекта не изменяются; всё новое живёт в `experiments/llm-bench/`.

Платформы: **L** — Linux + NVIDIA (основная), **W** — Windows 11 + WSL2 (Ubuntu; команды как L внутри WSL), **M** — macOS Apple Silicon (inference через llama.cpp; дообучение — MLX-LM, см. шаг 10M). PowerShell-варианты даны только там, где WSL не подходит.

---

## Шаг 0. Подготовка машины и окружения

- **Цель:** Python 3.12, зависимости стенда, сервер модели.
- **Предусловия:** репозиторий распакован; `python3.12 --version` работает; для обучения — драйвер NVIDIA (`nvidia-smi` показывает GPU; в WSL2 — драйвер ставится в Windows, внутри WSL ничего ставить не нужно).
- **Где:** L/W/M — терминал в корне репозитория.
- **Команды (L/W):**
  ```bash
  python3.12 -m venv .venv-bench && source .venv-bench/bin/activate
  pip install --upgrade pip
  pip install -r requirements-local.txt
  pip install -r experiments/llm-bench/requirements-base.txt
  # только для дообучения / hf_local (Linux, WSL2):
  pip install -r experiments/llm-bench/requirements-train.txt
  # опционально:
  pip install -r experiments/llm-bench/requirements-optional.txt
  pip freeze > experiments/llm-bench/checks/pip_freeze_$(date +%Y%m%d).txt
  ```
  **(M):** то же, но без `requirements-train.txt` (bitsandbytes нет); для inference установить llama.cpp: `brew install llama.cpp`.
  Сервер модели (один из): llama.cpp — `brew install llama.cpp` (M) или сборка/релиз `llama-server` с CUDA (L/W); vLLM — `pip install vllm` (L/W, отдельное venv); Ollama — установщик с сайта.
- **Ожидаемый вывод:** `pip install` без ошибок; `python -c "import yaml, jinja2"` молчит.
- **Проверка:** `python -c "import torch; print(torch.cuda.is_available())"` → `True` на L/W с GPU.
- **Если не получилось:** ошибка сборки bitsandbytes на Windows-native → работайте в WSL2; на M — пропустить train-зависимости.
- **Время:** 20–40 мин активного.
- **Артефакты:** `checks/pip_freeze_<дата>.txt`.
- **Реестр:** не пишется.
- **Дальше:** шаг 1.

## Шаг 1. Установка комплекта и проверка окружения

- **Цель:** каталог `experiments/llm-bench` на месте, стенд видит проект.
- **Предусловия:** шаг 0; архив `llm-bench-kit-v1.zip`.
- **Где:** корень репозитория.
- **Команды:**
  ```bash
  unzip -o llm-bench-kit-v1.zip -d /tmp/llm-bench-kit
  python3 /tmp/llm-bench-kit/apply_kit.py --repo . --kit /tmp/llm-bench-kit
  cd experiments/llm-bench
  export PYTHONPATH=.            # (W/L; в PowerShell: $env:PYTHONPATH=".")
  python -m bench check-env
  ```
- **Ожидаемый вывод:** JSON со `repo_root`, версиями пакетов; код 0.
- **Проверка:** `echo $?` → 0; в JSON `"repo_root"` = путь к `weak-signals`.
- **Если не получилось:** «корень репозитория не найден» → `export WS_REPO_ROOT=/полный/путь/weak-signals`; `apply_kit.py` отказался копировать (уже есть файлы) → удалите старый `experiments/llm-bench` или запустите с `--force`.
- **Время:** 5 мин.
- **Артефакты:** `experiments/llm-bench/`.
- **Реестр:** нет.
- **Дальше:** шаг 2.

## Шаг 2. Сборка корпуса C1+C3p, split, заморозка, валидация, mock-прогон

- **Цель:** детерминированный `examples.jsonl` (540), split-v1, замороженный test, работающий стенд.
- **Предусловия:** шаг 1; `ml/data/raw/dataset_normalized.csv` и `ml/data/labels/negatives_dev_b.yaml` на месте.
- **Где:** `experiments/llm-bench`, `PYTHONPATH=.`.
- **Команды:**
  ```bash
  python -m bench build-data && python -m bench split && python -m bench validate-data
  python -m bench freeze
  python -m bench smoke --limit 12
  python -m unittest discover -s tests -t .
  python -m bench report
  ```
- **Ожидаемый вывод:** `собрано: .../examples.jsonl`; `проблем: 0`; `заморожено: protocol/frozen_test.json`; два `run smoke__mock__...: completed`; `Ran 25 tests ... OK`.
- **Проверка:** `sha256sum data/build/examples.jsonl` = `083ff603ee5bdc19e6c8a0be10366891c3c4bad772fcef168e6eac7e0984578d`; в `data/build/splits/split-v1.json` test `ids_sha256` = `c038e30677444058d5c869af7e7f9f166ac11d0b80d9f07ba99696991ca5bb43`. Совпадение хешей = данные идентичны эталону комплекта.
- **Если не получилось:** хеш другой → проверьте версию `ml/dataset.py` и файлов данных (`git status`); `validate-data` нашёл утечки → не продолжать, прислать вывод.
- **Время:** 5 мин.
- **Артефакты:** `data/build/*`, `protocol/frozen_test.json`, `registry/registry.sqlite` (только mock-записи), `reports/*`.
- **Реестр:** runs `smoke__mock__*` с `is_mock=1` — не результат.
- **Дальше:** шаг 3.

## Шаг 3. Диагностика железа

- **Цель:** `config/hardware.json` и уровень (CPU/S/M/L/XL/APPLE).
- **Предусловия:** шаг 0.
- **Где:** та же оболочка.
- **Команды:** `python -m bench hw-preflight` (L/W/M). Дополнительно L/W: `nvidia-smi`, `df -h .`, `free -g`; M: `system_profiler SPHardwareDataType`.
- **Ожидаемый вывод:** JSON + строка `УРОВЕНЬ: <tier>` + оценки памяти по кандидатам.
- **Проверка:** уровень ≥ M → дообучение 8B возможно (QLoRA); L → LoRA bf16; CPU/S → только inference, обучение переносится на арендованный GPU (шаги 9–12 выполняются там, данные и конфиги те же).
- **Если не получилось:** `nvidia-smi` не найден в WSL2 → обновите драйвер NVIDIA в Windows; код возврата 3 = GPU не найден (это результат, а не ошибка).
- **Время:** 5 мин.
- **Артефакты:** `config/hardware.json`.
- **Реестр:** нет (файл прикладывается к возврату).
- **Дальше:** зафиксировать n кандидатов в `protocol/protocol_v1.yaml → candidates` (комментарием, без изменения версии) и перейти к шагу 4.

## Шаг 4. Preflight моделей (ревизии, лицензии, токены)

- **Цель:** `config/candidates.lock.json` с commit sha, размерами, длинами промптов в токенах.
- **Предусловия:** сеть; `huggingface_hub`; при необходимости `huggingface-cli login`.
- **Где:** та же оболочка.
- **Команды:**
  ```bash
  python -m bench model-preflight --candidates tlite-2.1 yagpt5-lite-8b qwen3-8b
  python -m bench model-preflight --candidates tlite-2.1 yagpt5-lite-8b qwen3-8b --download   # веса bf16 (~16 ГБ каждая) — только на L/W с диском ≥ 60 ГБ
  ```
  Лицензия YandexGPT: `python - <<'EOF'\nfrom huggingface_hub import hf_hub_download; print(open(hf_hub_download("yandex/YandexGPT-5-Lite-8B-instruct","LICENSE")).read()[:4000])\nEOF` — прочитать условия коммерческого использования и записать вывод в `checks/verification_log.md`.
- **Ожидаемый вывод:** по каждому кандидату `status: resolved`, `revision_sha`, `tokenizer_stats` с p95 длины промпта (ориентир: v1 ≈ 6–8k, v2 ≈ 7–9k токенов).
- **Проверка:** p95 промпта v2 + 2500 токенов ответа < `max_len` (8192 → при превышении уменьшите `bench_ctx`/примеры длиннее будут исключены адаптером с записью доли).
- **Если не получилось:** `gated: true` → принять условия на HF; ошибка токенизатора с `trust_remote_code` → это GigaChat, ожидаемо, включайте только после шага 9.
- **Время:** 10 мин + скачивание.
- **Артефакты:** `config/candidates.lock.json`; скопируйте `revision_sha` в `config/train/<candidate>.yaml → revision` и в `config/candidates.yaml → revision` (Найти: `revision: null` → Заменить: `revision: <sha>` для нужного кандидата).
- **Реестр:** `candidates` заполняется при первом eval.
- **Дальше:** шаг 5.

## Шаг 5. Запуск сервера модели для baseline (B)

- **Цель:** OpenAI-совместимый endpoint на `http://127.0.0.1:8000/v1`.
- **Предусловия:** веса (GGUF для llama.cpp/Ollama, HF для vLLM).
- **Где:** второй терминал (сервер работает постоянно).
- **Команды (по одному варианту на кандидата):**
  - llama.cpp (L/W/M): скачать GGUF `huggingface-cli download <gguf_repo> <файл q4_k_m или q8_0> --local-dir models/` и запустить
    `llama-server -m models/<файл>.gguf -c 16384 --port 8000 --jinja -ngl 99 --temp 0.2` (M: `-ngl 99` использует Metal). Для Qwen3-моделей добавьте `--reasoning-budget 0` (если ваша версия не знает флаг — стенд передаёт `chat_template_kwargs` в теле запроса; проверьте, что ответ не начинается с `<think>`).
  - vLLM (L/W): `vllm serve t-tech/T-lite-it-2.1 --revision <sha> --served-model-name t-lite-it-2.1 --max-model-len 16384 --port 8000 --gpu-memory-utilization 0.9`.
  - Ollama: `ollama pull <модель>`; base_url `http://127.0.0.1:11434/v1`; thinking отключается параметром `think: false` — добавьте в `config/candidates.yaml → endpoint.extra_body: {think: false}` для этого кандидата.
- **Ожидаемый вывод:** `curl -s http://127.0.0.1:8000/v1/models` возвращает JSON со списком моделей.
- **Проверка:** `curl -s http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"t-lite-it-2.1","messages":[{"role":"user","content":"Ответь JSON {\"ok\":true}"}],"max_tokens":20}'` → в `content` JSON без `<think>`.
- **Если не получилось:** OOM у vLLM → `--max-model-len 12288` или `--quantization` (fp8/awq); llama.cpp медленно → уменьшите `-c`, проверьте `-ngl`.
- **Время:** 15–30 мин на модель.
- **Артефакты:** лог сервера (сохраните версию: `llama-server --version` / `vllm --version`) в `checks/`.
- **Реестр:** нет.
- **Дальше:** шаг 6.

## Шаг 6. Baseline: arm A (текущий контур) и arm B (кандидаты до обучения) на validation

- **Цель:** первые измеримые числа.
- **Предусловия:** шаг 5 (для B); ключи проекта (для A в облаке) — `WS_YANDEX_API_KEY`/`WS_GIGACHAT_CREDENTIALS` из `.env` проекта.
- **Где:** `experiments/llm-bench`, `PYTHONPATH=.`.
- **Команды:**
  ```bash
  # B: кандидат через локальный сервер, оба pipeline
  python -m bench run-all --candidate tlite-2.1 --base-url http://127.0.0.1:8000/v1 --model t-lite-it-2.1
  # A (облако, как в проде) — YandexGPT через OpenAI-совместимый API
  export WS_YANDEX_API_KEY=<ключ>
  python -m bench baseline --candidate qwen3-8b --variant cloud-yandexgpt --pipeline v1_current --backend-kind openai_compat \
     --base-url https://llm.api.cloud.yandex.net/v1 --model "gpt://<folder_id>/yandexgpt/latest" --api-key-env WS_YANDEX_API_KEY --auth-scheme Api-Key \
     --notes "arm A: облачная модель проекта"
  # A (GigaChat через SDK проекта): --backend-kind gigachat_sdk --api-key-env WS_GIGACHAT_CREDENTIALS
  # A (локальный llama.cpp проекта, тот же GGUF, что в compose): поднимите его на другом порту и укажите --base-url
  python -m bench report
  ```
  Имя `--candidate` для облачного baseline — любой существующий ключ candidates.yaml (используется только для конфигурации); `--variant cloud-*` отличает запуск.
- **Ожидаемый вывод:** `run exp-2026-09__tlite-2.1__base__v1_current__validation__a1: completed; n=81 ...` и аналог для v2_card; в `reports/summary.csv` строки с числами.
- **Проверка:** `backend_error_mean` = 0; `valid_json_mean` > 0.9 (иначе проверьте, нет ли `<think>` в `runs/<run_id>/predictions.jsonl → raw_output`, и поддерживает ли сервер `response_format`: если нет — `json_mode: false` в candidates.yaml).
- **Если не получилось:** `HTTP 400 response_format` → в `config/candidates.yaml` для этого endpoint `json_mode: false`; ответы обрезаны (`possible_truncation`) → `--max-tokens 3000` для v2.
- **Время:** 81 примера × ~20–60 с ≈ 0.5–1.5 ч на pipeline на модель (машинное).
- **Артефакты:** `runs/<run_id>/predictions.jsonl`, `metrics.json`; `reports/*`.
- **Реестр:** runs со статусом `completed`/`partial`; при `partial` → `python -m bench eval ... --resume <run_id> --retry-errors`.
- **Дальше:** повторить для yagpt5-lite-8b и qwen3-8b; затем шаг 7.

## Шаг 7. Продуктивные данные C2 из БД и фактический baseline A

- **Цель:** реальные кандидаты + evidence + выданные нарративы.
- **Предусловия:** запущенный стек проекта (`docker compose up -d postgres`), пароль Postgres из `.env`.
- **Где:** корень репозитория (psql/docker), затем `experiments/llm-bench`.
- **Команды:**
  ```bash
  # вариант с psycopg (pip install "psycopg[binary]"):
  python -m bench export-db --dsn "postgresql://postgres:<пароль>@127.0.0.1:5599/weaksignals"
  # без psycopg: python -m bench export-db  → печатает команды psql (вариант A/B), выполнить их
  python -m bench convert-db --analysis-as-of $(date +%F) --merge
  python -m bench validate-data && python -m bench freeze     # split пересчитан: заморозить test заново ДО любых прогонов на test
  python -m bench import-prod-baseline
  python -m bench report
  ```
- **Ожидаемый вывод:** `C2: <N> примеров → .../examples_c2.jsonl; baseline-выходов <N>`; `добавлено в examples.jsonl: <N>`; run `exp-2026-09__prod_current__as-is__v1_current__all_c2__a1: completed`.
- **Проверка:** в `reports/summary.csv` у `prod_current` заполнены `v1_first_pass_accept_mean`, `language_ok_mean`; `data/build/examples_c2.manifest.json → generated_share` совпадает с долей GENERATED в demo.
- **Если не получилось:** пустая выгрузка → в БД нет завершённых job (выполните 2–3 запроса через UI/API и повторите); ошибка колонки → сверьте версию миграций (`\d orchestrator.result_items`).
- **Время:** 20 мин.
- **Артефакты:** `data/build/db_export/*`, `examples_c2.jsonl`, `c2_baseline_outputs.jsonl`; **не передавайте `body_text` третьим лицам без проверки условий источников**.
- **Реестр:** run `prod_current` = arm A на C2.
- **Дальше:** повторите шаг 6 для кандидатов на validation (теперь validation включает C2-примеры), затем шаг 8.

## Шаг 8. Teacher-targets для train и ручная проверка

- **Цель:** ≥ 150 верифицированных целевых карточек v2 для SFT.
- **Предусловия:** доступна сильная модель как teacher (облачный GigaChat/YandexGPT проекта, либо T-pro-it-2.1 на XL-железе, либо лучший из кандидатов по шагу 6 — это допустимо, но записывается как ограничение: self-distillation).
- **Где:** `experiments/llm-bench`.
- **Команды:**
  ```bash
  python -m bench teacher --teacher-id t1-yandexgpt --backend-kind openai_compat --base-url https://llm.api.cloud.yandex.net/v1 \
     --model "gpt://<folder_id>/yandexgpt/latest" --api-key-env WS_YANDEX_API_KEY --split train
  python -m bench teacher --teacher-id t1-yandexgpt --split validation
  ```
  Ручная проверка: откройте `data/build/teacher/t1-yandexgpt/accepted.jsonl` и `review_sheet.jsonl`; каждый участник ставит в свою колонку `ok` / `reject` (критерии — `human_eval/form_ru.md`, раздел «проверка targets»); затем
  `python -m bench teacher --teacher-id t1-yandexgpt --apply --only-verified`.
- **Ожидаемый вывод:** `teacher t1-...: принято N, отклонено M`; после apply — `targets применены: K`.
- **Проверка:** `python -m bench export-sft` показывает `{"train": K1, "validation": K2}`; K1 ≥ 150 к концу дня 4 (правило остановки в протоколе).
- **Если не получилось:** доля принятых < 50 % → посмотрите `rejected.jsonl → checks.hard`: чаще всего числа не из evidence — усилите feedback (уже есть 2-я попытка) или смените teacher.
- **Время:** машинное 1–2 ч; активное (проверка 2×~150 карточек по 3–4 мин) ≈ 8–10 ч на двоих — главный ресурс плана.
- **Артефакты:** `data/build/teacher/<id>/*`, `data/build/sft_messages/*`.
- **Реестр:** нет (данные), сведения о teacher — в manifest.json.
- **Дальше:** шаг 9.

## Шаг 9. Preflight обучения (10 проверок)

- **Цель:** убедиться, что обучение технически идёт на этом железе.
- **Предусловия:** уровень ≥ M; `requirements-train.txt`; `revision` в `config/train/<cand>.yaml`; targets ≥ 4.
- **Где:** `experiments/llm-bench` (L/W; на M — см. 10M).
- **Команды:** `python -m bench train --candidate tlite-2.1 --preflight` (затем для остальных).
- **Ожидаемый вывод:** `run ...sft-lora-v1-preflight__train__a1: completed; adapter ...; changed tensors k/k`.
- **Проверка:** в `runs/<run_id>/train_report.json`: `delta.tensors_changed > 0`, `peak_vram_gb` < VRAM − 2, `final_loss` конечен; `python -m bench verify-checkpoint <run_id>` → `PASS`.
- **Если не получилось:** `oom` → в yaml `max_len: 6144`, `micro_batch: 1`, `quantization: 4bit`; «адаптер не изменился» → проверьте `target_modules` по `model.named_modules()`; GigaChat: ошибка PEFT/`trust_remote_code` → исключить из обучения (остаётся inference-only).
- **Время:** 10–20 мин на модель.
- **Артефакты:** `runs/<run_id>/{adapter_init,adapter,train_report.json,verify_checkpoint.json}`.
- **Реестр:** run phase=train, статус completed/oom/failed с причиной.
- **Дальше:** шаг 10.

## Шаг 10. Полное дообучение

- **Цель:** адаптер LoRA на train с оценкой eval_loss на validation.
- **Предусловия:** шаг 9 PASS; targets применены.
- **Где:** L/W с GPU (или арендованный сервер: скопируйте `experiments/llm-bench` и `services/insight`, `libs/ws_common`, `tools/minischema.py`, `ml/` — стенд ищет корень по `services/insight/src`).
- **Команды:** `python -m bench train --candidate tlite-2.1` (в tmux/screen; прерванное продолжается `--resume-from runs/<run_id>/checkpoints/checkpoint-<N>`).
- **Ожидаемый вывод:** события `step N: loss=...` в реестре (`sqlite3 registry/registry.sqlite "select ts,message from events where run_id='<run_id>' order by id desc limit 5"`), в конце `completed`.
- **Проверка:** `learning_curve.json`: loss убывает, `eval_loss` не растёт на последних шагах; `verify-checkpoint <run_id>` PASS.
- **Если не получилось:** loss NaN → lr 5e-5, `max_grad_norm 0.5`; eval_loss растёт с первой эпохи → 1 эпоха; OOM посреди эпохи → resume с меньшим `max_len`.
- **Время:** машинное 1–4 ч на модель (зависит от GPU и числа targets); активное 15 мин.
- **Артефакты:** `runs/<run_id>/adapter`, кривые, отчёт.
- **Реестр:** run train + metrics (`final_loss`, `peak_vram_gb`, `adapter_tensors_changed`).
- **Дальше:** шаг 11.

**10M (macOS, MLX-LM, альтернативный путь):** `pip install mlx-lm`; `python -m bench export-sft`; `mlx_lm.lora --model t-tech/T-lite-it-2.1 --train --data data/build/sft_messages/v2_card --iters 600 --batch-size 1 --num-layers 16 --adapter-path runs/mlx-tlite`; затем оценка через `mlx_lm.server --model t-tech/T-lite-it-2.1 --adapter-path runs/mlx-tlite --port 8000` и шаг 12 через `openai_compat`. Записи в реестр делаются на шаге 12; preflight-проверки 1–10 для MLX выполняются вручную (tensors changed = сравните файлы адаптера до/после).

## Шаг 11. Проверка чекпойнта

- **Цель:** доказать, что обученное действительно загружается и отличается от базы.
- **Команды:** `python -m bench verify-checkpoint <train_run_id>`.
- **Ожидаемый вывод:** JSON `status: PASS`, `tensors_changed > 0`, `matches_registry: true`.
- **Если не получилось:** `matches_registry:false` → адаптер изменён после обучения; переобучить или зафиксировать причину.
- **Артефакты/реестр:** `verify_checkpoint.json`, metrics `verify_pass`. **Дальше:** шаг 12. (Время 5–10 мин.)

## Шаг 12. Оценка чекпойнта (arm C) на validation, выбор чекпойнта

- **Цель:** те же метрики, что для B, для модели с адаптером.
- **Предусловия:** шаг 11.
- **Команды:**
  ```bash
  python -m bench eval --candidate tlite-2.1 --variant sft-lora-v1 --pipeline v2_card --split validation \
     --backend-kind hf_local --adapter-path runs/<train_run_id>/adapter --load-in-4bit
  # или через сервер: vllm serve <model> --enable-lora --lora-modules sft=runs/<train_run_id>/adapter ... затем --backend-kind openai_compat --model sft
  python -m bench report --baseline <run_id arm A на validation или base того же кандидата>
  ```
- **Ожидаемый вывод:** run `...sft-lora-v1__v2_card__validation__a1: completed`; в report.md таблица «Разницы относительно baseline» с Δ и ДИ.
- **Проверка:** `critical_mean` не выше base; сравнение только на общих example_id (n в таблице).
- **Если не получилось:** hf_local медленно → уменьшите `--limit` для промежуточных чекпойнтов, полный прогон — для 1–2 лучших; несколько чекпойнтов → `--adapter-path runs/<id>/checkpoints/checkpoint-N` (каждый — свой variant, например `sft-lora-v1-ck50`).
- **Время:** 0.5–1.5 ч на чекпойнт.
- **Артефакты/реестр:** run eval с `adapter_sha256`, metrics `delta_vs_baseline_*`. **Дальше:** шаг 13.

## Шаг 13. Слепая экспертная оценка

- **Цель:** человеческая оценка 30 примеров × 2–3 run, каппа.
- **Команды:**
  ```bash
  python -m bench human pack --packet p1 --runs <run_A> <run_B_base> <run_C_sft> --n 30
  # оценщики заполняют human_eval/packets/p1/scores_template.csv (каждый — свою копию, колонка rater = имя), не открывая KEY_*.json
  python -m bench human ingest --packet p1 --scores human_eval/packets/p1/scores_rater1.csv
  python -m bench human ingest --packet p1 --scores human_eval/packets/p1/scores_rater2.csv
  python -m bench human agreement --packet p1
  ```
- **Ожидаемый вывод:** `пакет p1: 30 примеров × 3 run`; после ingest — число оценок; agreement → каппа по критериям.
- **Проверка:** каппа ≥ 0.4 по `evidence_support` и `russian_language`; иначе — калибровочная встреча и повторная оценка 10 примеров.
- **Время:** активное 2 × 3–4 ч.
- **Артефакты/реестр:** `human_scores` в реестре, файлы пакетов. **Дальше:** шаг 14.

## Шаг 14. Финальный прогон на закрытом test (один раз на конфигурацию)

- **Цель:** цифры для gate.
- **Предусловия:** выбран один чекпойнт на кандидата по validation; `protocol/frozen_test.json` соответствует данным (после шага 7 заморозка обновлена).
- **Команды:** те же `eval`/`baseline`, добавив `--split test --allow-test` — для arm A, B (base) и C (выбранный адаптер) каждого кандидата; затем `python -m bench report --baseline <run A test>`.
- **Ожидаемый вывод:** run `...__test__a1: completed`, n = число test-примеров; при повторе — attempt a2 (история сохраняется, в отчёте обе).
- **Проверка:** «хеш test или версия протокола не совпадают» → запуск запрещён: не правьте данные, выясните причину.
- **Если не получилось:** результат на test хуже validation → это результат; менять чекпойнт/промпт после просмотра test нельзя без новой версии протокола (`protocol_version` + `freeze`).
- **Время:** 1–2 ч машинного на кандидата.
- **Артефакты/реестр:** runs на test, `reports/*`. **Дальше:** шаг 15.

## Шаг 15. Отчёт и gate

- **Цель:** заполнить `reports/report.md` числами и применить правила `protocol_v1.yaml → decision_gate`.
- **Команды:** `python -m bench report --baseline <run_id A на test>`; затем скопировать таблицы в итоговый отчёт хакатона; в `checks/verification_log.md` дописать выполненные команды с кодами возврата.
- **Проверка:** в таблице гипотез нет «NOT RUN» для H1, H3–H7; для остальных — обоснование.
- **Время:** 2–3 ч активного. **Дальше:** шаг 16.

## Шаг 16. Что вернуть

Перечень — `REPORT_RU.md §21`: `config/hardware.json`, `config/candidates.lock.json`, `registry/registry.sqlite`, `reports/*`, `runs/*/` (без весов, если тяжело), `data/build/*` (без body_text при ограничениях), `human_eval/packets/*`, `protocol/frozen_test.json`, дополненный `checks/verification_log.md`, логи серверов моделей.

---

### Интеграция локальной модели в рабочий контур (только для arm A «локальный вариант»; продукт не меняется)

В `.env` проекта (имена переменных — по `services/insight/src/insight/config.py`, там же значения по умолчанию): `WS_LLM_LOCAL_ENABLED=true`, `WS_LOCAL_LLM_BASE_URL=http://<хост>:<порт>/v1`, `WS_LOCAL_LLM_MODEL=<served_model_name>`, при желании `WS_LLM_PRIMARY_PROVIDER=local_llamacpp` и `WS_LLM_FALLBACK_PROVIDER=none`. Для compose-профиля: `docker compose --profile local-llm up -d` (GGUF по умолчанию — `yandexgpt-5-lite-8b-instruct-q4_k_m.gguf`, ctx 8192). Обратите внимание на дефект D1 (`localhost:8090` vs `local-llm:8000`): вне compose адрес задавайте явно.
