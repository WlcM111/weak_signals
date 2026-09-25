"""Команды последовательного обучения A → B (регистрируются в `ml.cli`)."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ml.config import REPO_ROOT, MlSettings
from ml.seq import metrics as M
from ml.seq.stages import VARIANTS, load_checkpoint, train_stage_a, train_stage_b


def register(sub: Any) -> None:
    """Подкоманды seq-*."""
    build = sub.add_parser("seq-build-b", help="пересборка датасета B v1 из зафиксированных прогонов "
                                               "(validation_report_v1.json → run_files)")
    build.add_argument("--project-root", default=str(REPO_ROOT), help="каталог с analytics-*.txt и own-topics-*.json")
    sub.add_parser("seq-validate-b", help="валидация датасета B: схема, split, группы, даты, классы")
    stage_a = sub.add_parser("seq-stage-a", help="Stage A: обучение на A-train (организаторы + явные вспомогательные)")
    stage_a.add_argument("--embedder", choices=("e5", "hashing"), default="e5")
    stage_a.add_argument("--run-id")
    stage_b = sub.add_parser("seq-stage-b", help="Stage B: продолжение из контрольной точки A на датасете B")
    stage_b.add_argument("--parent", required=True, help="путь к stage_a.json")
    stage_b.add_argument("--variant", choices=VARIANTS, default="seq_laplace")
    stage_b.add_argument("--alpha", type=float, default=1.0)
    stage_b.add_argument("--rho", type=float, default=0.3)
    stage_b.add_argument("--l2", type=float, default=3.0)
    stage_b.add_argument("--l2sp", type=float, default=3.0)
    stage_b.add_argument("--w-a", type=float, default=1.0)
    stage_b.add_argument("--embedder", choices=("e5", "hashing"), default="e5")
    experiments = sub.add_parser("seq-experiments", help="полный протокол: варианты, абляции, кривые, holdout")
    experiments.add_argument("--embedder", choices=("e5", "hashing"), default="e5")
    experiments.add_argument("--run-id")
    experiments.add_argument("--quick", action="store_true", help="smoke: сокращённые сетки и бутстрэп")
    evaluate = sub.add_parser("seq-evaluate", help="однократная оценка контрольной точки B на holdout и A-test")
    evaluate.add_argument("--checkpoint", required=True)
    evaluate.add_argument("--embedder", choices=("e5", "hashing"), default="e5")
    export = sub.add_parser("seq-export", help="экспорт контрольной точки в model-store (v2)")
    export.add_argument("--checkpoint", required=True)
    export.add_argument("--version", required=True, help="wsclf-YYYY.MM.DD-N")
    export.add_argument("--activate", action="store_true", help="сделать активной (прежняя — в rollback/)")
    activate = sub.add_parser("seq-activate", help="сделать активной существующую версию (откат)")
    activate.add_argument("version")


def _run_dir(settings: MlSettings, run_id: str | None, prefix: str) -> Path:
    run_id = run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{prefix}"
    path = settings.reports_dir / "seq" / run_id
    path.mkdir(parents=True, exist_ok=False)
    return path


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=1, default=str))


def command_build_b(settings: MlSettings, args: argparse.Namespace) -> int:
    from ml.dataset_b.build import build  # noqa: PLC0415

    base = settings.data_dir / "dataset_b"
    pinned = json.loads((base / "validation_report_v1.json").read_text(encoding="utf-8"))["run_files"]
    report = build(Path(args.project_root), base, run_files=pinned)
    _print({k: report[k] for k in ("status", "rows_total", "supervised", "uncertain", "labels", "by_split", "problems")})
    return 0 if report["status"] == "ok" else 1


def command_validate_b(settings: MlSettings, args: argparse.Namespace) -> int:
    from ml.dataset_b.build import _validate  # noqa: PLC0415

    from ml.seq.data import dataset_b_file  # noqa: PLC0415

    base = settings.data_dir / "dataset_b"
    main_file = dataset_b_file()
    uncertain_file = main_file.removesuffix(".jsonl") + "_uncertain.jsonl"
    rows = [json.loads(x) for name in (main_file, uncertain_file)
            for x in (base / name).read_text(encoding="utf-8").splitlines() if x.strip()]
    problems = _validate(rows)
    problems += [f"{r['sample_id']}: неопределённая строка в обучающем файле"
                 for r in rows if r["label"] is None and r["label_kind"] != "UNCERTAIN"]
    summary: dict[str, Any] = {"rows": len(rows), "problems": problems, "by_split_label": {}, "label_kinds": {},
                               "groups": len({r["group_id"] for r in rows})}
    for row in rows:
        key = f"{row['split']}|{row['label']}"
        summary["by_split_label"][key] = summary["by_split_label"].get(key, 0) + 1
        summary["label_kinds"][row["label_kind"]] = summary["label_kinds"].get(row["label_kind"], 0) + 1
    _print(summary)
    return 0 if not problems else 1


def command_stage_a(settings: MlSettings, args: argparse.Namespace) -> int:
    from ml.seq.experiments import LAMBDAS_A, _a_metrics, build_context  # noqa: PLC0415

    out = _run_dir(settings, args.run_id, f"stage-a-{args.embedder}")
    ctx = build_context(settings, args.embedder, settings.seed, settings.reports_dir / "experiments" / "cache")
    y = np.asarray([o.label for o in ctx.a_train], dtype=float)
    ck = train_stage_a(ctx.X["a_train"], y, [o.group_id for o in ctx.a_train], LAMBDAS_A, settings.seed,
                       ctx.projection, ctx.featurizer.embedding_model, ctx.featurizer.glossary,
                       {k: ctx.meta[k] for k in ("a_file", "a_sha256", "a_train_ids", "a_train_sha256",
                                                 "a_excluded_linked_to_b", "a_origins")})
    ck.metrics = {"a_dev": _a_metrics(np.asarray([o.label for o in ctx.a_dev], dtype=float), ck.logits(ctx.X["a_dev"]))}
    sha = ck.save(out / "stage_a.json")
    _print({"checkpoint": str(out / "stage_a.json"), "sha256": sha, "checkpoint_id": ck.checkpoint_id,
            "l2": ck.params["l2"], "a_dev": ck.metrics["a_dev"], "a_train_rows": len(ctx.a_train)})
    return 0


def command_stage_b(settings: MlSettings, args: argparse.Namespace) -> int:
    from ml.seq.experiments import _a_metrics, _ids_sha, build_context  # noqa: PLC0415

    parent, parent_sha = load_checkpoint(Path(args.parent))
    if parent.stage != "A":
        raise SystemExit("родительская контрольная точка должна быть Stage A")
    ctx = build_context(settings, args.embedder, settings.seed, settings.reports_dir / "experiments" / "cache",
                        projection=parent.projection)
    if ctx.featurizer.embedding_model != parent.embedding_model:
        raise SystemExit(f"эмбеддер {ctx.featurizer.embedding_model} не совпадает с Stage A {parent.embedding_model}")
    if [o.sample_id for o in ctx.a_train] != parent.data["a_train_ids"] or ctx.meta["a_sha256"] != parent.data["a_sha256"]:
        raise SystemExit("A-train или файл A отличаются от использованных в Stage A: перенос состояния некорректен")
    params = {"b_only": {"l2": args.l2}, "seq_finetune_early": {"l2": args.l2, "maxiter": 5.0},
              "seq_finetune": {"l2": args.l2}, "seq_l2sp": {"l2sp": args.l2sp}, "seq_laplace": {"alpha": args.alpha},
              "seq_laplace_replay": {"alpha": args.alpha, "rho": args.rho},
              "joint": {"l2": args.l2, "w_a": args.w_a}}[args.variant]
    y_b = np.asarray([o.label for o in ctx.b_dev], dtype=float)
    y_a = np.asarray([o.label for o in ctx.a_train], dtype=float)
    a_ids = [o.sample_id for o in ctx.a_train]
    oof = np.zeros(len(y_b))
    for fold in sorted({o.split for o in ctx.b_dev}):
        valid = np.array([o.split == fold for o in ctx.b_dev])
        ck = train_stage_b(args.variant, params, parent, parent_sha, ctx.X["b_dev"][~valid], y_b[~valid],
                           ctx.X["a_train"], y_a, a_ids, {"b_train_sha256": _ids_sha(ctx.b_dev, ~valid)})
        oof[valid] = ck.logits(ctx.X["b_dev"][valid])
    cal = M.platt(oof, y_b)
    probs = M.calibrated(oof, cal)
    final = train_stage_b(args.variant, params, parent, parent_sha, ctx.X["b_dev"], y_b, ctx.X["a_train"], y_a, a_ids,
                          {"b_file": ctx.meta["b_file"], "b_sha256": ctx.meta["b_sha256"],
                           "b_train_sha256": _ids_sha(ctx.b_dev, None), "b_train": "all dev folds"})
    final.calibration, final.threshold = cal, M.best_f1_threshold(probs, y_b)
    final.metrics = {"dev_oof": {**M.summary(y_b, oof, [o.topic for o in ctx.b_dev]),
                                 "at_dev_threshold": M.at_threshold(y_b, probs, final.threshold)},
                     "a_dev_parent": parent.metrics.get("a_dev"),
                     "a_dev": _a_metrics(np.asarray([o.label for o in ctx.a_dev], dtype=float),
                                         final.logits(ctx.X["a_dev"]))}
    out = Path(args.parent).parent
    path = out / f"stage_b_{args.variant}.json"
    sha = final.save(path)
    _print({"checkpoint": str(path), "sha256": sha, "parent_id": final.parent_id, "parent_sha256": parent_sha,
            "uses_parent_state": final.params["uses_parent_state"], "started_from": final.params["started_from"],
            "distance_from_parent": final.params["distance_from_parent"],
            "replay_rows": len(final.params["replay_ids"]), "dev_oof": final.metrics["dev_oof"],
            "a_dev_parent": final.metrics["a_dev_parent"], "a_dev_after_b": final.metrics["a_dev"]})
    return 0


def command_experiments(settings: MlSettings, args: argparse.Namespace) -> int:
    from ml.seq.experiments import run_experiments  # noqa: PLC0415

    out = run_experiments(settings, args.embedder, settings.seed, args.run_id, args.quick)
    report = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    _print({"run_dir": str(out), "chosen_on_dev": report["chosen_on_dev"],
            "recommended_sequential": report["recommended_sequential"],
            "holdout_uses": report["final"]["holdout_uses_including_this_run"]})
    return 0


def command_evaluate(settings: MlSettings, args: argparse.Namespace) -> int:
    from ml.seq.experiments import _a_metrics, build_context  # noqa: PLC0415

    ck, sha = load_checkpoint(Path(args.checkpoint))
    ctx = build_context(settings, args.embedder, settings.seed, settings.reports_dir / "experiments" / "cache",
                        projection=ck.projection)
    y_h = np.asarray([o.label for o in ctx.b_hold], dtype=float)
    probs = ck.probability(ctx.X["b_hold"])
    report = {"checkpoint_id": ck.checkpoint_id, "sha256": sha,
              "b_holdout": {**M.summary(y_h, probs, [o.topic for o in ctx.b_hold]), "brier": M.brier(y_h, probs),
                            "ece_5bins": M.ece(y_h, probs), "at_threshold": M.at_threshold(y_h, probs, ck.threshold)},
              "a_test": _a_metrics(np.asarray([o.label for o in ctx.a_test], dtype=float), ck.logits(ctx.X["a_test"]))}
    log = settings.reports_dir / "experiments" / "holdout_usage.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"checkpoint": ck.checkpoint_id, "at": datetime.now(UTC).isoformat()}) + "\n")
    report["holdout_uses_total"] = sum(1 for _ in log.open(encoding="utf-8"))
    _print(report)
    return 0


def command_export(settings: MlSettings, args: argparse.Namespace) -> int:
    from ml.seq.export import describe, export, query_similarity_floor  # noqa: PLC0415

    from ml.seq.stages import load_checkpoint as _load  # noqa: PLC0415

    import hashlib  # noqa: PLC0415

    from ml.seq.data import dataset_b_file, load_b  # noqa: PLC0415

    b_path = settings.data_dir / "dataset_b" / dataset_b_file()
    trained_on = _load(Path(args.checkpoint))[0].data or {}
    dev_ids = [o.sample_id for o in load_b(b_path) if o.split.startswith("dev_fold_")]
    same_rows = trained_on.get("b_train_sha256") == hashlib.sha256("|".join(dev_ids).encode()).hexdigest()
    if trained_on.get("b_train") == "all dev folds" and not same_rows:
        print(f"экспорт отклонён: контрольная точка обучена не на {b_path.name}; "
              "задайте WS_DATASET_B_FILE того же прогона протокола")
        return 1
    floor = query_similarity_floor(b_path)
    decision = ((_load(Path(args.checkpoint))[0].metrics or {}).get("acceptance") or {}).get("decision")
    if args.activate and decision != "accept":
        print(f"активация отклонена: решение приёмки = {decision!r}; версия будет экспортирована без активации")
        args.activate = False
    target = export(Path(args.checkpoint), settings.model_store_dir, args.version, floor, args.activate)
    _print({"exported": str(target), "activated": args.activate, "min_query_similarity": floor, **describe(target)})
    return 0


def command_activate(settings: MlSettings, args: argparse.Namespace) -> int:
    from ml.seq.export import activate_version  # noqa: PLC0415

    backup = activate_version(settings.model_store_dir, args.version)
    _print({"active": args.version, "previous_active_backup": str(backup) if backup else None})
    return 0


COMMANDS = {
    "seq-build-b": command_build_b, "seq-validate-b": command_validate_b, "seq-stage-a": command_stage_a,
    "seq-stage-b": command_stage_b, "seq-experiments": command_experiments, "seq-evaluate": command_evaluate,
    "seq-export": command_export, "seq-activate": command_activate,
}
