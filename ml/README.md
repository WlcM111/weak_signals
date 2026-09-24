# ml — конвейер обучения модели слабых сигналов

Офлайн-инструмент (`python -m ml.cli`), который готовит данные, считает признаки **тем же кодом,
что и analyzer**, обучает и честно оценивает интерпретируемый классификатор, калибрует его и
экспортирует артефакт в `model-store`, откуда его забирает analyzer.

## Команды

| Команда | Что делает |
|---|---|
| `build-negatives` | строит строки отрицательного класса из `data/labels/negatives_dev_b.yaml`, пишет лист перекрёстной разметки, считает каппу при наличии второй разметки |
| `build-dataset` | собирает 100 позитивов организаторов + негативы команды в `data/labels/dataset_<версия>.jsonl` и валидирует по `training_row.schema.json` |
| `eda` | профиль набора в `ml/reports/eda.{json,md}` |
| `enrich` | ENRICHMENT-коллекции через collector (или `--fixtures` без сети) в `data/enriched/` |
| `leak-check` | контроль утечки маркеров разметки |
| `train` | baselines, CV, калибровка, порог, отчёт, экспорт артефакта |
| `evaluate <файл>` | скоринг произвольного csv/xlsx в формате исходного датасета |
| `export <версия>` | сделать версию активной |

## Полный прогон в рабочем контуре

```bash
docker compose --profile ml run --rm trainer build-dataset
docker compose --profile ml run --rm trainer enrich          # нужен collector, ≈1.5–3 ч
docker compose --profile ml run --rm trainer train           # эмбеддер e5, ≈10 мин на CPU
docker compose up -d analyzer                                # подхватит model-store/active
```

Первый запуск `train` скачивает модель эмбеддингов e5 (≈1.1 ГБ) в том `hf-cache`; последующие
запуски работают из кеша. Если контур без интернета, кеш нужно принести заранее.

## Быстрый прогон без сети и тяжёлых моделей

```bash
python -m ml.cli build-dataset
python -m ml.cli leak-check
python -m ml.cli train --embedder hashing --smoke
```

`--embedder hashing` — детерминированный хеширующий эмбеддер вместо e5. Он нужен, чтобы конвейер
и тесты работали без загрузки моделей. Идентификатор такой модели (`hashing-char-ngram-256`)
попадает в манифест, и **analyzer откажется** активировать такой артефакт при работе с e5 —
это защита от случайной подмены, а не ошибка.

## Протокол оценки

Стратифицированный holdout 20 % используется ровно один раз, в конце. Выбор гиперпараметров,
калибровка Платта и порог (максимум F1 при precision ≥ `WS_TRAIN_MIN_PRECISION`) считаются на
`RepeatedStratifiedKFold(5×5)` по обучающим 80 %. Эталонные центроиды `emb_sim_*` пересчитываются
внутри каждого фолда по его обучающей части — иначе эмбеддинговые признаки видят метки валидации.

Опорные решения: B0 (разность лексических признаков), B1 (TF-IDF + LR), B2 (эмбеддинги + LR),
B3 (LightGBM, если пакет установлен). Они нужны, чтобы понимать, что даёт модель на 24 признаках
сверх простых решений.

## Артефакт

`model-store/<версия>/`: `classifier.joblib`, `scaler.joblib`, `calibrator.joblib`,
`centroids.npz`, `rules.json`, `feature_defaults.json`, `manifest.json` с sha256 всех файлов.
Копия версии кладётся в `active/`. Analyzer проверяет контрольные суммы перед загрузкой.

## Конфигурация

`WS_DATA_DIR`, `WS_MODEL_STORE_DIR`, `WS_COLLECTOR_ADDR`, `WS_TRAIN_SEED=20260915`,
`WS_TRAIN_HOLDOUT_SHARE=0.2`, `WS_TRAIN_CV_REPEATS=5`, `WS_TRAIN_CV_FOLDS=5`,
`WS_TRAIN_MIN_PRECISION=0.80`, `WS_TRAIN_MAX_RULE_EXCLUSION=0.05`, `WS_EMBEDDING_MODEL`,
`WS_MLFLOW_TRACKING_URI`, `HF_HOME`, `WS_LEXICON_DIR`, `WS_STAGE_RULES_PATH`.

MLflow используется, если установлен и задан URI; иначе запуски пишутся в `ml/reports/runs.jsonl`
— воспроизводимость не должна зависеть от наличия трекера.

## Тесты

```bash
PYTHONPATH=ml/src:services/analyzer/src:libs/ws_common/src \
  python -m unittest discover -s ml/tests -t ml
```
