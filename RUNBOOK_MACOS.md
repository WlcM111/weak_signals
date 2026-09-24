# RUNBOOK: переработка ML (A → B) на Mac

Машина: Apple M4 Pro, 24 ГБ, macOS, zsh, Docker Desktop (CPU). Проект: `/Users/zorinmihail/Desktop/weak-signals`. Все команды — в **одном** окне Терминала, по порядку. Строки блоков копируются целиком; пояснения вынесены из блоков. Ни одна команда не печатает секреты из `.env`.

## 1. Проверка машины и проекта

Включает комментарии в zsh (на случай вставки строк с `#`), переходит в проект и показывает версии:

```zsh
setopt interactivecomments
cd /Users/zorinmihail/Desktop/weak-signals
uname -m
sw_vers -productVersion
git --version
docker version --format '{{.Server.Os}}/{{.Server.Arch}} {{.Server.Version}}'
docker compose version
git status --short | head -20
git log --oneline -3
```

Ожидается `arm64`, Docker `linux/arm64`. Если `git log` пишет, что репозитория нет, зафиксируйте текущее состояние — это точка отката:

```zsh
git init
git add -A
git commit -m "state before ml-rework v2"
```

Если `git status` показывает незакоммиченные изменения, закоммитьте их отдельным коммитом (`git add -A && git commit -m "local changes"`), иначе откат патча будет сложнее.

## 2. Резервные копии (модели, БД, настройки)

```zsh
export BK="$HOME/ws-backups/$(date +%Y%m%d-%H%M)"
mkdir -p "$BK"
export VOL=$(docker volume ls --format '{{.Name}}' | grep -E '(^|_)model-store$' | head -1)
echo "$VOL"
docker run --rm -v "$VOL":/m:ro -v "$BK":/b alpine tar czf /b/model-store.tgz -C /m .
docker compose up -d postgres
docker compose exec -T postgres pg_dump -U postgres -d weaksignals -Fc > "$BK/weaksignals.dump"
cp .env "$BK/env.backup" && chmod 600 "$BK/env.backup"
ls -lh "$BK"
```

`$VOL` должен быть непустым (обычно `weak-signals_model-store`). Три файла в `$BK` — точка полного отката (разд. 16).

## 3. Прошлый `ml_rework.patch`: применён ли, и откат

Признак применённого старого патча — файл `ml/tests/test_group_split.py`:

```zsh
test -f ml/tests/test_group_split.py && echo "СТАРЫЙ ПАТЧ ПРИМЕНЁН" || echo "старый патч не применён"
```

Если применён — откатите его (файл старого патча лежит там, куда вы его скачивали, например `~/Downloads/ml_rework.patch`):

```zsh
git apply -R --check -v ~/Downloads/ml_rework.patch
git apply -R ~/Downloads/ml_rework.patch
test -f ml/tests/test_group_split.py && echo "откат не выполнен" || echo "старый патч откатан"
```

Если `--check` сообщает о конфликтах (файлы меняли после патча), вернитесь к коммиту до старого патча: найдите его в `git log --oneline` и выполните `git checkout <коммит> -- .`, затем `git status` — лишние новые файлы старого патча удалите вручную по списку из `git status`.

## 4. Применение нового патча

Скачайте `weak-signals-ml-rework.patch` и `SHA256SUMS` в `~/Downloads`, сверьте сумму и примените:

```zsh
cp ~/Downloads/weak-signals-ml-rework.patch .
shasum -a 256 weak-signals-ml-rework.patch
grep weak-signals-ml-rework.patch ~/Downloads/SHA256SUMS
git apply --check -v weak-signals-ml-rework.patch
git apply weak-signals-ml-rework.patch
git diff --check && echo "пробелы в порядке"
git status --short | wc -l
test -f RUNBOOK_MACOS.md && test -f ml/src/ml/seq/stages.py && echo "патч применён"
git add -A && git commit -m "ml-rework v2 (A -> B)"
```

Суммы в двух строках должны совпасть. Если `--check` падает — не применяйте патч; проверьте разд. 3 и что каталог совпадает с архивом `weak_signals_stable.zip`.

## 5. Настройки `.env` (без вывода секретов)

Секреты этого `.env` были переданы в чат — **перевыпустите** ключи LLM-провайдеров, `WS_API_KEY` и пароли БД у их владельцев и впишите новые значения в `.env` в редакторе (`open -e .env`). Затем добавьте новые переменные, если их нет, и проверьте только несекретные строки:

```zsh
grep -q '^WS_CANDIDATE_JUDGE_ORDER=' .env || printf '\nWS_CANDIDATE_JUDGE_ORDER=ml\n' >> .env
grep -q '^WS_ML_SCORE_ABLATION=' .env || printf 'WS_ML_SCORE_ABLATION=none\n' >> .env
grep -E '^WS_(CANDIDATE_JUDGE_(ORDER|ENABLED)|ML_SCORE_ABLATION|MIN_CANDIDATE_QUERY_SIM|JOB_DEADLINE[A-Z_]*)=' .env
```

`WS_CANDIDATE_JUDGE_ORDER=ml` — порядок выдачи задаёт локальная модель, LLM только исключает; `llm` — прежнее поведение. `WS_ML_SCORE_ABLATION` штатно `none`.

## 6. Сборка и запуск

```zsh
docker compose build collector analyzer orchestrator-api orchestrator-worker
docker compose --profile ml build trainer
docker compose up -d
sleep 20
docker compose ps
docker compose logs analyzer --since 5m | grep -E 'model\.(artifacts_verified|activated)' | tail -3
```

Все сервисы — `running`/`healthy`. Активной пока остаётся прежняя модель v1.

## 7. Тесты

Тесты ML внутри образа trainer (все зависимости уже в нём):

```zsh
docker compose --profile ml run --rm --entrypoint python -e PYTHONPATH=/app/ml/src:/app/services/analyzer/src:/app/libs/ws_common/src:/app/libs/ws_contracts/src trainer -m unittest discover -s ml/tests -t ml
```

Ожидается `OK` (тест пересборки датасета B пропускается в образе — журналы прогонов лежат в корне проекта, а не в образе). Полный набор всех сервисов локально (необязательно, ставит зависимости в `.venv`):

```zsh
brew list uv >/dev/null 2>&1 || brew install uv
uv sync --all-packages
uv run make test
```

## 8. Проверка датасета B

```zsh
docker compose --profile ml run --rm trainer seq-validate-b
```

Ожидается `"problems": []`, 171 строка, split `dev_fold_0..4` и `holdout`. Пересборка из журналов (при добавлении прогонов или правке меток) — `docker compose --profile ml run --rm -v "$PWD":/project:ro trainer seq-build-b --project-root /project`.

## 9. Базовая линия (текущая система)

Зафиксируйте активную модель и снимите выдачу прежнего поведения по шести темам датасета:

```zsh
docker compose exec -T analyzer sh -c 'cat /models/active/manifest.json' | grep -E '"(model_version_id|model_family|feature_schema_version|embedding_model)"'
sed -i '' 's/^WS_CANDIDATE_JUDGE_ORDER=.*/WS_CANDIDATE_JUDGE_ORDER=llm/' .env
docker compose up -d orchestrator-api orchestrator-worker
sleep 15
python3 tools/theme_analytics.py
ls -t analytics-*.txt | head -1
```

Последний `analytics-*.txt` — базовая линия. Верните порядок ML:

```zsh
sed -i '' 's/^WS_CANDIDATE_JUDGE_ORDER=.*/WS_CANDIDATE_JUDGE_ORDER=ml/' .env
docker compose up -d orchestrator-api orchestrator-worker
```

## 10. Stage A (e5)

```zsh
docker compose --profile ml run --rm trainer seq-stage-a --embedder e5 --run-id mac-e5-a1
```

Вывод: путь `/app/ml/reports/seq/mac-e5-a1/stage_a.json` (на Mac — `ml/reports/seq/mac-e5-a1/stage_a.json`), `sha256`, выбранный `l2` и ROC-AUC на A-dev. Первый запуск считает e5-эмбеддинги (1–3 мин; модель берётся из тома `hf-cache`, заполненного analyzer).

## 11. Stage B из контрольной точки A

```zsh
docker compose --profile ml run --rm trainer seq-stage-b --parent /app/ml/reports/seq/mac-e5-a1/stage_a.json --variant seq_laplace --alpha 1 --embedder e5
```

В выводе проверьте: `parent_sha256` равен `sha256` из разд. 10, `uses_parent_state: true`, `started_from: "parent"`, `a_dev_after_b` не ниже `a_dev_parent` более чем на 0,05. Команда отказывается работать, если эмбеддер, файл A или A-train отличаются от Stage A.

## 12. Полный протокол экспериментов

```zsh
docker compose --profile ml run --rm trainer seq-experiments --embedder e5 --run-id mac-e5-full-1
python3 tools/ml_rework/render_results.py ml/reports/experiments/mac-e5-full-1
open ml/reports/experiments/mac-e5-full-1/RESULTS.md
```

Запускайте один раз: каждый запуск вычисляет holdout и пишется в `ml/reports/experiments/holdout_usage.jsonl`. Повтор с новым `--run-id` допустим только с записью причины. Решение — раздел «Решение приёмки» в `RESULTS.md` (`accept` или `reject_keep_current_model`).

## 13. Экспорт и активация (только при `accept`)

Имя контрольной точки — `stage_b_<chosen_on_dev>.json` из вывода разд. 12:

```zsh
ls ml/reports/experiments/mac-e5-full-1/checkpoints/
docker compose --profile ml run --rm trainer seq-export --checkpoint /app/ml/reports/experiments/mac-e5-full-1/checkpoints/stage_b_seq_l2sp.json --version wsclf-2026.09.25-1 --activate
docker compose restart analyzer
sleep 20
docker compose logs analyzer --since 2m | grep -E 'model\.(artifacts_verified|activated)|feature_schema' | tail -3
```

При решении, отличном от `accept`, экспорт выполнится без активации (сообщение «активация отклонена») — прежняя модель остаётся. Для **временного** живого сравнения v2 с v1 можно активировать версию вручную и обязательно вернуть прежнюю после разд. 14:

```zsh
docker compose --profile ml run --rm trainer seq-activate wsclf-2026.09.25-1
docker compose restart analyzer
```

## 14. Живые прогоны и абляции

Каждый прогон — выдача по шести темам (`tools/theme_analytics.py`) или по темам из файла (`tools/quality/run_topics.py`). Holdout-темы B:

```zsh
printf '%s\n' "квантовые сенсоры для навигации" "перспективные решения в финтехе" "цифровые двойники в энергетике" > /tmp/holdout_topics.txt
python3 tools/quality/run_topics.py /tmp/holdout_topics.txt
python3 tools/theme_analytics.py
```

Абляция вклада модели (analyzer): константа, затем перестановка, затем возврат:

```zsh
sed -i '' 's/^WS_ML_SCORE_ABLATION=.*/WS_ML_SCORE_ABLATION=constant/' .env && docker compose up -d analyzer && sleep 20
python3 tools/theme_analytics.py
sed -i '' 's/^WS_ML_SCORE_ABLATION=.*/WS_ML_SCORE_ABLATION=shuffle/' .env && docker compose up -d analyzer && sleep 20
python3 tools/theme_analytics.py
sed -i '' 's/^WS_ML_SCORE_ABLATION=.*/WS_ML_SCORE_ABLATION=none/' .env && docker compose up -d analyzer
```

Абляции LLM (orchestrator): порядок LLM и отключённая смысловая проверка, затем возврат:

```zsh
sed -i '' 's/^WS_CANDIDATE_JUDGE_ORDER=.*/WS_CANDIDATE_JUDGE_ORDER=llm/' .env && docker compose up -d orchestrator-api orchestrator-worker && sleep 15
python3 tools/theme_analytics.py
grep -q '^WS_CANDIDATE_JUDGE_ENABLED=' .env || printf 'WS_CANDIDATE_JUDGE_ENABLED=true\n' >> .env
sed -i '' 's/^WS_CANDIDATE_JUDGE_ORDER=.*/WS_CANDIDATE_JUDGE_ORDER=ml/; s/^WS_CANDIDATE_JUDGE_ENABLED=.*/WS_CANDIDATE_JUDGE_ENABLED=false/' .env && docker compose up -d orchestrator-api orchestrator-worker && sleep 15
python3 tools/theme_analytics.py
sed -i '' 's/^WS_CANDIDATE_JUDGE_ENABLED=.*/WS_CANDIDATE_JUDGE_ENABLED=true/' .env && docker compose up -d orchestrator-api orchestrator-worker
```

Каждый `analytics-*.txt` сравните с базовой линией разд. 9 по совпадению выдачи с темой и строками датасета; новые кандидаты holdout-тем размечайте по `ml/data/dataset_b/LABELING_PROTOCOL.md` — это материал для B v2.

## 15. Контроль 1200 с и ресурсов

```zsh
docker stats --no-stream
docker compose logs orchestrator-worker --since 3h | grep -cE 'DEADLINE_EXCEEDED'
docker compose logs orchestrator-worker --since 3h | grep -E '"stage.expand".*used_fallback' | tail -3
docker compose logs collector --since 3h | grep -E 'http\.retry_after_exceeds|http\.retry' | tail -5
grep -E 'время|duration|DEADLINE' "$(ls -t analytics-*.txt | head -1)" | head -20
```

Каждое задание должно завершиться (успех, частичный результат или `DEADLINE_EXCEEDED`) не позже 1200 с от приёма; память контейнеров — в пределах лимитов compose (trainer 6 ГБ).

## 16. Сбор результатов и откат

Архив результатов (эксперименты, контрольные точки, живые прогоны за 3 суток, несекретные настройки, версии, SHA-256):

```zsh
bash tools/ml_rework/collect_results.sh mac-e5-a1 mac-e5-full-1
```

Скрипт откажется создавать архив, если найдёт строки, похожие на ключи или пароли. Откат — от быстрого к полному:

```zsh
sed -i '' 's/^WS_CANDIDATE_JUDGE_ORDER=.*/WS_CANDIDATE_JUDGE_ORDER=llm/' .env && docker compose up -d orchestrator-api orchestrator-worker
docker compose exec -T analyzer sh -c 'ls /models; ls /models/rollback 2>/dev/null'
docker compose --profile ml run --rm trainer seq-activate rollback/active-ГГГГММДДTЧЧММССZ
docker compose restart analyzer
git apply -R --check weak-signals-ml-rework.patch && git apply -R weak-signals-ml-rework.patch
docker run --rm -v "$VOL":/m -v "$BK":/b alpine sh -c 'rm -rf /m/* && tar xzf /b/model-store.tgz -C /m'
```

Во второй строке подставьте имя резервной копии из списка `/models/rollback` (или прежнюю версию, например `wsclf-2026.09.16-2`). После отката кода пересоберите образы (разд. 6). Восстановление БД из дампа — только если нужно вернуть результаты прогонов: `docker compose exec -T postgres pg_restore -U postgres -d weaksignals --clean < "$BK/weaksignals.dump"`.
