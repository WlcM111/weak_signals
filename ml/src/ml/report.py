"""Формирование отчёта об оценке модели (`docs/ml/evaluation_report.md`) и графиков."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TARGET_ACCURACY = 0.80
ACCEPTANCE_ACCURACY = 0.75


def write_report(
    path: Path,
    result: Any,
    dataset_version: str,
    model_version_id: str,
    embedding_model: str,
    enrichment_stats: dict[str, Any],
    rules_exclusion: tuple[float, dict[str, int]],
    leak_summary: str,
    negatives_warnings: Sequence[str],
    style_diagnostic: dict[str, Any] | None = None,
    text_lengths: dict[str, Any] | None = None,
) -> Path:
    """Пишет отчёт с фактическими метриками; целевые значения отмечаются отдельно."""
    test = result.test_metrics
    confidence = result.test_confidence
    lines = [
        "# Отчёт об оценке модели слабых сигналов",
        "",
        f"Дата: {datetime.now(UTC):%Y-%m-%d}. Датасет: `{dataset_version}`. "
        f"Модель: `{model_version_id}`. Эмбеддер: `{embedding_model}`.",
        "",
        "## 1. Данные",
        "",
        f"- строк: **{result.dataset_stats['rows']}** "
        f"(позитивов {result.dataset_stats['positives']}, негативов {result.dataset_stats['negatives']})",
        f"- доля положительного класса: {result.dataset_stats['balance']:.2f}",
        f"- holdout-тест: {len(result.test_index)} строк, обучающая часть: {len(result.train_index)}",
        f"- обогащение коллекциями: {enrichment_stats.get('enriched', 0)} строк из "
        f"{enrichment_stats.get('total', 0)}; без коллекции — признаки «пустой коллекции»",
        "",
        "## 2. Протокол оценки",
        "",
        "Стратифицированный holdout 20 % использован ровно один раз — для финальной таблицы ниже. "
        "Выбор гиперпараметров, калибровка и порог получены на `RepeatedStratifiedKFold` по "
        "обучающим 80 %. Эталонные центроиды `emb_sim_*` пересчитываются внутри каждого фолда "
        "только по его обучающей части, поэтому эмбеддинговые признаки не видят меток валидации.",
        "",
        "## 3. Метрики на отложенном тесте (фактические)",
        "",
        "| Метрика | Значение | 95 % ДИ (бутстрэп, 1000) |",
        "|---|---|---|",
    ]
    for name in ("accuracy", "precision", "recall", "f1"):
        interval = confidence.get(name)
        interval_text = f"{interval[0]:.3f} – {interval[1]:.3f}" if interval else "—"
        lines.append(f"| {name} | **{test[name]:.3f}** | {interval_text} |")
    lines += [
        f"| roc_auc | {test['roc_auc']:.3f} | — |",
        f"| pr_auc | {test['pr_auc']:.3f} | — |",
        f"| n | {test['n']} | — |",
        "",
        f"Порог решения: **{result.model.threshold:.3f}** "
        f"({result.calibration['threshold_rule']}).",
        f"Ошибка калибровки ECE на CV-предсказаниях: **{result.calibration['ece']:.3f}**.",
        "",
        "Матрица ошибок (строки — факт 0/1, столбцы — предсказание 0/1):",
        "",
        "```",
        json.dumps(result.confusion),
        "```",
        "",
        "## 4. Кросс-валидация и опорные решения",
        "",
        "| Решение | F1 (CV) | Accuracy (CV) |",
        "|---|---|---|",
        f"| **M: LR elastic-net (24 признака)** | **{result.cv_metrics['f1_mean']:.3f} ± "
        f"{result.cv_metrics['f1_std']:.3f}** | {result.cv_metrics['accuracy_mean']:.3f} ± "
        f"{result.cv_metrics['accuracy_std']:.3f} |",
    ]
    for name, values in result.baselines.items():
        if values.get("skipped"):
            lines.append(f"| {name} | не запускался (пакет не установлен) | — |")
            continue
        f1 = values.get("f1_mean", values.get("f1", 0.0))
        accuracy = values.get("accuracy_mean", values.get("accuracy", 0.0))
        lines.append(f"| {name} | {f1:.3f} | {accuracy:.3f} |")
    share, reasons = rules_exclusion
    lines += [
        "",
        f"Гиперпараметры выбранной модели: {result.model.params}.",
        "",
        "## 5. Разрезы на тесте",
        "",
        "| Разрез | Группа | n | Accuracy |",
        "|---|---|---|---|",
    ]
    for slice_name, groups in result.slices.items():
        for group, values in groups.items():
            lines.append(f"| {slice_name} | {group} | {values['n']} | {values['accuracy']:.3f} |")
    lines += [
        "",
        "## 6. Правила исключения",
        "",
        f"После калибровки порогов правила §12.6 исключают **{share:.1%}** позитивов обучающей части "
        f"(требование ТЗ — не более 5 %). Распределение по причинам: "
        f"{reasons if reasons else 'ни одно правило не сработало'}.",
        "",
        "## 7. Проверка утечки разметки",
        "",
        f"{leak_summary}",
        "",
        "## 7a. Стилевая отделимость классов (критично для интерпретации метрик)",
        "",
    ]
    tfidf = result.baselines.get("B1_tfidf_lr", {}).get("f1_mean")
    if text_lengths:
        lines += [
            f"Длина текста наблюдения: позитивы {text_lengths['positives']['min']}–"
            f"{text_lengths['positives']['max']} символов (медиана "
            f"{text_lengths['positives']['median']}), негативы "
            f"{text_lengths['negatives']['min']}–{text_lengths['negatives']['max']} "
            f"(медиана {text_lengths['negatives']['median']}).",
            "",
        ]
        if "length_only_accuracy_raw" in text_lengths:
            lines += [
                "Контроль ложного признака: модель с единственным признаком «длина текста» даёт accuracy "
                f"{text_lengths['length_only_accuracy_raw']:.3f} на исходных текстах и "
                f"{text_lengths['length_only_accuracy_train']:.3f} на текстах, поданных в обучение "
                f"(выравнивание длины: {'включено' if text_lengths.get('aligned') else 'выключено'}). "
                "Чем ближе второе число к доле большего класса, тем меньше метрики зависят от стиля авторов.",
                "",
            ]
    if tfidf is not None:
        lines += [
            f"Опорное решение B1 (TF-IDF по словам, без признаков реестра) даёт F1 {tfidf:.3f}. "
            + (
                "Это означает, что классы различимы по лексике и стилю текста, а не только по "
                "содержательным признакам: позитивы написаны методологами заказчика, негативы — "
                "командой, и авторский стиль сам по себе является предиктором. Метрики раздела 3 "
                "следует читать как верхнюю оценку, не переносимую на скрытую проверку "
                "организаторов."
                if tfidf >= 0.85
                else "Значение ниже качества модели на признаках реестра, то есть решение опирается "
                "не только на лексику."
            ),
            "",
        ]
    if style_diagnostic:
        lines += [
            "Контрольный прогон «только название» (вариант B схемы обучающей строки, описания "
            "удалены у обоих классов, `--text-mode title`): "
            f"accuracy {style_diagnostic['test']['accuracy']:.3f}, "
            f"F1 {style_diagnostic['test']['f1']:.3f}, CV F1 "
            f"{style_diagnostic['cv_f1_mean']:.3f}. Разница с основным прогоном показывает вклад "
            "описаний (и их стиля) в качество.",
            "",
        ]
    lines += [
        "## 8. Соответствие целевым значениям",
        "",
        f"Целевая точность кейса — не ниже {TARGET_ACCURACY:.0%} (граница приёмки "
        f"{ACCEPTANCE_ACCURACY:.0%}). Фактическая accuracy на отложенном тесте — "
        f"**{test['accuracy']:.1%}**, F1 — **{test['f1']:.1%}**. "
        + (
            "Целевое значение достигнуто на этом наборе данных."
            if test["accuracy"] >= ACCEPTANCE_ACCURACY
            else "Целевое значение НЕ достигнуто; см. ограничения ниже."
        ),
        "",
        "## 9. Ограничения",
        "",
    ]
    limitations = [
        "Отрицательный класс размечен командой, а не организаторами: метрики измеряют качество на "
        "собственной выборке негативов и не переносятся автоматически на скрытую проверку.",
        "Тексты классов написаны разными авторами и различаются длиной и стилем (раздел 7a); "
        "до выравнивания описаний негативов по длине и тональности метрики завышены.",
        f"Эмбеддер `{embedding_model}`: если это не модель из манифеста analyzer, артефакт "
        "предназначен только для офлайн-прогона конвейера.",
    ]
    limitations += list(negatives_warnings)
    if enrichment_stats.get("enriched", 0) < enrichment_stats.get("total", 0):
        limitations.append(
            "Коллекционные и энциклопедические признаки вычислены не для всех строк: без доступа "
            "к collector они принимают значения «пустой коллекции», и их вклад в модель занижен."
        )
    lines += [f"{index}. {text}" for index, text in enumerate(limitations, start=1)]
    lines.append("")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_plots(result: Any, reports_dir: Path) -> list[Path]:
    """Диаграмма надёжности и матрица ошибок; без matplotlib — пропускается."""
    try:
        import matplotlib  # noqa: PLC0415 - необязательная зависимость

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415
    except ImportError:
        return []
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    bins = result.calibration["bins"]
    if bins:
        figure, axes = plt.subplots(figsize=(4, 4))
        axes.plot([0, 1], [0, 1], "--", color="grey", linewidth=1)
        axes.plot(
            [item["mean_probability"] for item in bins],
            [item["observed_rate"] for item in bins],
            marker="o",
        )
        axes.set_xlabel("средняя предсказанная вероятность")
        axes.set_ylabel("доля позитивов")
        axes.set_title("Диаграмма надёжности (CV)")
        figure.tight_layout()
        path = reports_dir / "calibration.png"
        figure.savefig(path, dpi=120)
        plt.close(figure)
        created.append(path)

    figure, axes = plt.subplots(figsize=(3.4, 3.4))
    matrix = result.confusion
    axes.imshow(matrix, cmap="Blues")
    for row_index, row in enumerate(matrix):
        for column_index, value in enumerate(row):
            axes.text(column_index, row_index, str(value), ha="center", va="center")
    axes.set_xticks([0, 1], ["предсказано 0", "предсказано 1"])
    axes.set_yticks([0, 1], ["факт 0", "факт 1"])
    axes.set_title("Матрица ошибок (тест)")
    figure.tight_layout()
    path = reports_dir / "confusion_matrix.png"
    figure.savefig(path, dpi=120)
    plt.close(figure)
    created.append(path)
    return created
