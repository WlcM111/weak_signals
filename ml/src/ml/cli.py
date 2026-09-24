"""Composition root ML-конвейера: `python -m ml.cli <команда>` (§9 HANDOFF_ML_PIPELINE).

Команды идемпотентны по выходным файлам: существующее не пересчитывается без `--force`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ml import dataset as dataset_module
from ml import eda, enrich, evaluate, export, negatives, report, train
from ml.adapters.embedders import build_embedder
from ml.adapters.model_store_fs import FileSystemModelStoreWriter
from ml.adapters.tracker import build_tracker
from ml.config import MlSettings
from ml.features import FeatureContext, empty_collection_defaults
from ml.leak_check import length_only_accuracy, run_leak_check
from ml.seq import cli as seq_cli
from ws_common.logging import configure_logging, get_logger

PASSAGE_PREFIX = "passage: "
SMOKE_ROWS = 40
log = get_logger("ml.cli")


def _context(settings: MlSettings) -> FeatureContext:
    """Реестр признаков и лексиконы — те же файлы, что читает analyzer."""
    return FeatureContext.load(
        settings.feature_registry_path,
        settings.lexicon_dir,
        settings.stage_rules_path,
        datetime.now(UTC),
    )


def _dataset_path(settings: MlSettings, version: str) -> Path:
    """Путь итогового набора обучения."""
    return settings.labels_dir / f"dataset_{version}.jsonl"


def _load_dataset(settings: MlSettings, version: str) -> list[dataset_module.TrainingRow]:
    """Читает собранный набор; понятная ошибка, если его ещё нет."""
    path = _dataset_path(settings, version)
    if not path.is_file():
        raise SystemExit(
            f"набор {path} не найден — сначала выполните `python -m ml.cli build-dataset`"
        )
    return dataset_module.read_jsonl(path)


def command_eda(settings: MlSettings, args: argparse.Namespace) -> int:
    """Профиль набора и краткая сводка в `ml/reports/`."""
    rows = _load_dataset(settings, args.dataset_version)
    path = eda.write_profile(rows, settings.reports_dir)
    print(f"профиль сохранён: {path}")
    return 0


def command_build_negatives(settings: MlSettings, args: argparse.Namespace) -> int:
    """Строит строки отрицательного класса из YAML-списка и шаблон для второго аннотатора."""
    result = negatives.load_negatives(Path(args.source))
    jsonl = settings.labels_dir / "negatives_dev_b.jsonl"
    dataset_module.write_jsonl(jsonl, result.rows)
    template = settings.labels_dir / "annotations_dev_b.csv"
    negatives.write_annotations_template(result.rows, template, Path(args.source))
    print(f"негативов: {len(result.rows)} → {jsonl}")
    print(f"подтипы: {result.by_subtype}")
    print(f"области: {result.by_domain}")
    print(f"лист разметки: {template}")
    for warning in result.warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {warning}")
    if args.second_annotation:
        second = negatives.read_second_annotation(Path(args.second_annotation))
        agreement = negatives.agreement_report(list(result.rows), second)
        print(f"согласие аннотаторов: {agreement}")
    return 0


def command_build_dataset(settings: MlSettings, args: argparse.Namespace) -> int:
    """Собирает позитивы из датасета организаторов и негативы команды в один JSONL."""
    positives = dataset_module.read_positive_rows(
        settings.raw_dir / "dataset_normalized.csv", settings.leak_patterns_path
    )
    negative_result = negatives.load_negatives(Path(args.source))
    rows = [*positives, *negative_result.rows]
    problems = dataset_module.validate_rows(rows, settings.training_row_schema_path)
    if problems:
        for problem in problems[:20]:
            print(f"ОШИБКА СХЕМЫ: {problem}")
        return 1
    path = _dataset_path(settings, args.dataset_version)
    written = dataset_module.write_jsonl(path, rows)
    print(f"набор собран: {written} строк ({len(positives)} позитивов, {len(negative_result.rows)} негативов)")
    print(f"файл: {path}")
    for warning in negative_result.warnings:
        print(f"ПРЕДУПРЕЖДЕНИЕ: {warning}")
    return 0


def command_enrich(settings: MlSettings, args: argparse.Namespace) -> int:
    """ENRICHMENT-коллекции для строк набора через collector или фикстуры."""
    rows = _load_dataset(settings, args.dataset_version)
    if args.fixtures:
        enricher: Any = enrich.FixtureEnricher(Path(args.fixtures))
    else:
        from ml.adapters.collector_grpc import CollectorEnricher  # noqa: PLC0415 - требует grpcio

        enricher = CollectorEnricher(settings.collector_addr, settings.enrichment_timeout_seconds)
    collected, failed = enrich.enrich_rows(
        rows, enricher, args.dataset_version, settings.enriched_dir, force=args.force
    )
    print(f"собрано коллекций: {collected}, ошибок: {failed}")
    return 2 if failed else 0


def command_leak_check(settings: MlSettings, args: argparse.Namespace) -> int:
    """Проверка утечки маркеров разметки в тексты позитивов."""
    rows = _load_dataset(settings, args.dataset_version)
    _, markers = dataset_module.load_leak_patterns(settings.leak_patterns_path)
    result = run_leak_check(rows, markers, settings.seed)
    print(result.summary)
    raw_accuracy = length_only_accuracy(rows, settings.seed)
    aligned_accuracy = length_only_accuracy(dataset_module.align_text_lengths(rows), settings.seed)
    print(
        f"контроль длины текста: точность по одной длине {raw_accuracy:.3f} на исходных текстах, "
        f"{aligned_accuracy:.3f} после выравнивания (обучение по умолчанию идёт на выровненных)"
    )
    return 0 if result.passed else 1


def command_train(settings: MlSettings, args: argparse.Namespace) -> int:
    """Обучение, честная оценка, экспорт артефакта и отчёт."""
    rows = _load_dataset(settings, args.dataset_version)
    if args.smoke:
        rows = _stratified_subset(rows, SMOKE_ROWS, settings.seed)
    if args.text_mode == "title":
        # вариант B схемы обучающей строки: только название, без описания — диагностика того,
        # сколько качества даёт содержание помимо стиля и длины текстов разных авторов
        rows = [replace(row, description="") for row in rows]
    raw_length_accuracy = length_only_accuracy(rows, settings.seed)
    if args.align_length and args.text_mode == "full":
        # обучение на выровненных по длине описаниях: иначе модель учит длину и стиль автора
        rows = dataset_module.align_text_lengths(rows)
    aligned_length_accuracy = length_only_accuracy(rows, settings.seed)
    context = _context(settings)
    embedder = build_embedder(args.embedder, settings.embedding_model, str(settings.hf_home))
    vectors = np.asarray(embedder.encode([row.text for row in rows], PASSAGE_PREFIX), dtype=np.float32)
    enrichments = [enrich.load_enrichment(settings.enriched_dir, row.row_id) for row in rows]
    enrichment_stats = {
        "total": len(rows),
        "enriched": sum(1 for item in enrichments if item and item.get("documents")),
    }
    tracker = build_tracker(settings.mlflow_tracking_uri, settings.reports_dir)
    tracker.start_run(f"train-{args.dataset_version}")
    tracker.log_params(
        {
            "dataset_version": args.dataset_version,
            "embedder": embedder.model_name,
            "seed": settings.seed,
            "holdout_share": settings.holdout_share,
            "cv": f"{settings.cv_repeats}x{settings.cv_folds}",
            "min_precision": settings.min_precision,
            "smoke": args.smoke,
        }
    )
    result = train.train_model(
        rows=rows,
        context=context,
        vectors=vectors,
        enrichments=enrichments,
        seed=settings.seed,
        holdout_share=settings.holdout_share,
        cv_repeats=settings.cv_repeats,
        cv_folds=settings.cv_folds,
        min_precision=settings.min_precision,
        smoke=args.smoke,
    )
    tracker.log_metrics({f"test_{key}": value for key, value in result.test_metrics.items()})
    tracker.log_metrics({f"cv_{key}": value for key, value in result.cv_metrics.items()})

    positives = [
        result.static_values[index]
        for index in result.train_index
        if rows[index].label == 1
    ]
    thresholds, share, reasons = export.calibrate_rule_thresholds(
        positives, settings.max_positive_exclusion_share
    )
    defaults = empty_collection_defaults(context, result.static_values)
    _, markers = dataset_module.load_leak_patterns(settings.leak_patterns_path)
    leak = run_leak_check(rows, markers, settings.seed)

    version_id = args.version or export.next_version_id(settings.model_store_dir)
    manifest = export.build_manifest(
        version_id=version_id,
        dataset_version=args.dataset_version,
        embedding_model=embedder.model_name,
        threshold=result.model.threshold,
        test_metrics=result.test_metrics,
        cv_metrics=result.cv_metrics,
    )
    diagnostic_path = settings.reports_dir / "style_diagnostic.json"
    if args.text_mode == "title":
        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        diagnostic_path.write_text(
            json.dumps(
                {
                    "text_mode": "title",
                    "test": result.test_metrics,
                    "cv_f1_mean": result.cv_metrics["f1_mean"],
                    "baseline_tfidf_f1": result.baselines.get("B1_tfidf_lr", {}).get("f1_mean"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    diagnostics = (
        json.loads(diagnostic_path.read_text(encoding="utf-8"))
        if diagnostic_path.is_file() and args.text_mode == "full"
        else None
    )
    report_path = report.write_report(
        settings.docs_dir / "evaluation_report.md",
        result,
        args.dataset_version,
        version_id,
        embedder.model_name,
        enrichment_stats,
        (share, reasons),
        leak.summary,
        negatives.load_negatives(Path(args.source)).warnings if Path(args.source).is_file() else (),
        style_diagnostic=diagnostics,
        text_lengths={
            **_text_lengths(rows),
            "length_only_accuracy_raw": raw_length_accuracy,
            "length_only_accuracy_train": aligned_length_accuracy,
            "aligned": bool(args.align_length and args.text_mode == "full"),
        },
    )
    plots = report.write_plots(result, settings.reports_dir)
    if not args.no_export:
        directory = FileSystemModelStoreWriter(settings.model_store_dir).export(
            version_id,
            {
                "classifier": result.model.classifier,
                "scaler": result.model.scaler,
                "calibrator": result.model.calibrator,
                "weak_centroid": result.model.weak_centroid,
                "mature_centroid": result.model.mature_centroid,
                "rules": export.build_rules_payload(thresholds, defaults),
            },
            manifest,
        )
        print(f"артефакт: {directory}")
        tracker.log_artifact(directory / "manifest.json")
    tracker.log_artifact(report_path)
    for plot in plots:
        tracker.log_artifact(plot)
    tracker.end_run()
    print(
        "тест: "
        + ", ".join(f"{key}={value:.3f}" for key, value in result.test_metrics.items() if key != "n")
    )
    print(f"CV F1: {result.cv_metrics['f1_mean']:.3f} ± {result.cv_metrics['f1_std']:.3f}")
    print(f"порог: {result.model.threshold:.3f}; правила исключают {share:.1%} позитивов")
    print(f"отчёт: {report_path}")
    return 0


def command_evaluate(settings: MlSettings, args: argparse.Namespace) -> int:
    """Скоринг произвольного файла активной моделью из `model-store`."""
    from analyzer.adapters.outbound.config_loader import load_feature_registry  # noqa: PLC0415
    from analyzer.adapters.outbound.model_store import FileSystemModelStore  # noqa: PLC0415

    registry = load_feature_registry(settings.feature_registry_path)
    bundle = FileSystemModelStore(settings.model_store_dir).load_active(registry)
    context = _context(settings)
    embedder = build_embedder(args.embedder, settings.embedding_model, str(settings.hf_home))
    if bundle.version.embedding_model != embedder.model_name:
        print(
            f"ПРЕДУПРЕЖДЕНИЕ: модель обучена с эмбеддером {bundle.version.embedding_model}, "
            f"используется {embedder.model_name} — скоринг несопоставим"
        )
    records = evaluate.read_table(Path(args.path))
    patterns, _ = dataset_module.load_leak_patterns(settings.leak_patterns_path)
    rows = evaluate.rows_from_table(records, patterns)
    scored = evaluate.score_rows(rows, context, embedder, bundle, bundle.thresholds)
    output = settings.reports_dir / f"scores_{Path(args.path).stem}.csv"
    evaluate.write_scores(output, scored)
    summary = evaluate.summarize(scored, bundle.threshold, has_labels=args.with_labels)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"таблица скорингов: {output}")
    return 0


def command_export(settings: MlSettings, args: argparse.Namespace) -> int:
    """Повторная публикация существующей версии в `active`."""
    source = settings.model_store_dir / args.version
    if not source.is_dir():
        raise SystemExit(f"версия {source} не найдена")
    import shutil  # noqa: PLC0415

    active = settings.model_store_dir / "active"
    if active.exists():
        shutil.rmtree(active)
    shutil.copytree(source, active)
    print(f"активная версия: {args.version}")
    return 0


def _text_lengths(rows: list[dataset_module.TrainingRow]) -> dict[str, Any]:
    """Медианы и диапазоны длины текста по классам — индикатор стилевой отделимости."""
    positives = sorted(len(row.text) for row in rows if row.label == 1)
    negatives_length = sorted(len(row.text) for row in rows if row.label == 0)
    def stats(values: list[int]) -> dict[str, int]:
        """Минимум, медиана, максимум."""
        return {
            "min": values[0] if values else 0,
            "median": values[len(values) // 2] if values else 0,
            "max": values[-1] if values else 0,
        }
    return {"positives": stats(positives), "negatives": stats(negatives_length)}


def _stratified_subset(
    rows: list[dataset_module.TrainingRow], size: int, seed: int
) -> list[dataset_module.TrainingRow]:
    """Подвыборка с сохранением баланса классов для быстрых прогонов."""
    generator = np.random.default_rng(seed)
    positives = [row for row in rows if row.label == 1]
    negatives_rows = [row for row in rows if row.label == 0]
    half = max(2, size // 2)
    chosen = [
        *[positives[index] for index in generator.permutation(len(positives))[:half]],
        *[negatives_rows[index] for index in generator.permutation(len(negatives_rows))[:half]],
    ]
    return chosen


def build_parser() -> argparse.ArgumentParser:
    """Разбор аргументов командной строки."""
    parser = argparse.ArgumentParser(prog="ml.cli", description="ML-конвейер Weak Signals")
    parser.add_argument("--dataset-version", default="ds-2026.09.19-v2", help="версия набора обучения")
    parser.add_argument(
        "--source",
        default=str(MlSettings().labels_dir / "negatives_dev_b.yaml"),
        help="YAML-список кандидатов-негативов",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("eda", help="профиль обучающего набора")
    build = sub.add_parser("build-negatives", help="строки негативов и лист перекрёстной разметки")
    build.add_argument("--second-annotation", help="CSV разметки второго аннотатора (row_id,label)")
    sub.add_parser("build-dataset", help="сборка итогового набора обучения")
    enrich_parser = sub.add_parser("enrich", help="ENRICHMENT-коллекции для строк набора")
    enrich_parser.add_argument("--fixtures", help="каталог записанных коллекций вместо collector")
    enrich_parser.add_argument("--force", action="store_true", help="пересобрать существующие")
    sub.add_parser("leak-check", help="проверка утечки маркеров разметки")
    train_parser = sub.add_parser("train", help="обучение, оценка и экспорт артефакта")
    train_parser.add_argument("--embedder", choices=("e5", "hashing"), default="e5")
    train_parser.add_argument("--smoke", action="store_true", help="быстрый прогон на 40 строках")
    train_parser.add_argument("--version", help="идентификатор версии модели")
    train_parser.add_argument("--no-export", action="store_true", help="не писать артефакт")
    train_parser.add_argument(
        "--text-mode",
        choices=("full", "title"),
        default="full",
        help="full: название + описание (как на инференсе); title: только название (диагностика стиля)",
    )
    train_parser.add_argument(
        "--no-align-length",
        dest="align_length",
        action="store_false",
        help="не выравнивать длину описаний классов (по умолчанию выравнивание включено)",
    )
    evaluate_parser = sub.add_parser("evaluate", help="скоринг произвольного файла")
    evaluate_parser.add_argument("path", help="csv/xlsx в формате исходного датасета")
    evaluate_parser.add_argument("--embedder", choices=("e5", "hashing"), default="e5")
    evaluate_parser.add_argument("--with-labels", action="store_true", help="в файле есть колонка label")
    export_parser = sub.add_parser("export", help="сделать версию активной")
    export_parser.add_argument("version", help="идентификатор версии в model-store")
    seq_cli.register(sub)
    return parser


COMMANDS = {
    "eda": command_eda,
    "build-negatives": command_build_negatives,
    "build-dataset": command_build_dataset,
    "enrich": command_enrich,
    "leak-check": command_leak_check,
    "train": command_train,
    "evaluate": command_evaluate,
    "export": command_export,
    **seq_cli.COMMANDS,
}


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI."""
    args = build_parser().parse_args(argv)
    configure_logging("trainer", "INFO", "json")
    settings = MlSettings()
    return COMMANDS[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
