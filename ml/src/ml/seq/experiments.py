"""Протокол A → B: dev-CV по темам, выбор по PR-AUC, абляции, кривые обучения, однократная оценка holdout."""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from analyzer.adapters.outbound.config_loader import load_lexicons
from analyzer.domain.features_v2 import FEATURE_GROUPS_V2, FEATURE_NAMES_V2

from ml.adapters.embedders import build_embedder
from ml.seq import metrics as M
from ml.seq.data import Obs, cross_links, dataset_b_file, load_a, load_b, sha256_file
from ml.seq.featurize import Featurizer
from ml.seq.stages import PROTECTED, VARIANTS, Checkpoint, train_stage_a, train_stage_b

LAMBDAS_A = (0.3, 1.0, 3.0, 10.0)
GRID: dict[str, list[dict[str, float]]] = {
    "b_only": [{"l2": v} for v in (1.0, 3.0, 10.0, 30.0)],
    "seq_finetune_early": [{"l2": 3.0, "maxiter": 5.0}],
    "seq_finetune": [{"l2": 3.0}],
    "seq_l2sp": [{"l2sp": v} for v in (1.0, 3.0, 10.0, 30.0)],
    "seq_laplace": [{"alpha": v} for v in (0.3, 1.0, 3.0, 10.0)],
    "seq_laplace_replay": [{"alpha": a, "rho": r} for a in (0.3, 1.0, 3.0) for r in (0.3, 1.0)],
    "joint": [{"l2": l2, "w_a": v} for l2 in (3.0, 10.0) for v in (0.3, 1.0)],
}
FORGETTING_TOLERANCE = 0.05
A_FILE = "dataset_ds-2026.09.19-v2.jsonl"
ABLATION_GROUPS = ("relevance", "specificity", "lexical", "metadata", "embedding")


@dataclass(slots=True)
class Context:
    """Данные, признаки и происхождение одного прогона."""

    a_train: list[Obs]
    a_dev: list[Obs]
    a_test: list[Obs]
    b_dev: list[Obs]
    b_hold: list[Obs]
    links: list[tuple[str, str, str]]
    featurizer: Featurizer
    X: dict[str, np.ndarray]
    meta: dict[str, Any]
    projection: Any = None


def load_glossary(path: Path) -> dict[str, str]:
    """Глоссарий ru→en (тот же файл, что у резервного расширения запроса)."""
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(k).lower(): str(v) for k, v in (payload.get("terms", payload) or {}).items()}


def build_context(settings, embedder_kind: str, seed: int, cache_dir: Path, projection=None) -> Context:  # noqa: ANN001
    """Чтение A и B, split, связи A↔B, эмбеддинги, проекция A-train и матрицы признаков."""
    a_path = settings.labels_dir / A_FILE
    b_path = settings.data_dir / "dataset_b" / dataset_b_file()
    glossary_path = settings.lexicon_dir.parent / "glossary_ru_en.yaml"
    rows_a, rows_b = load_a(a_path, seed), load_b(b_path)
    links = cross_links(rows_a, rows_b)
    linked = {a for a, _, _ in links}
    a_train = [o for o in rows_a if o.split == "train" and o.sample_id not in linked]
    a_dev = [o for o in rows_a if o.split == "dev"]
    a_test = [o for o in rows_a if o.split == "test"]
    b_dev = [o for o in rows_b if o.split.startswith("dev_fold_")]
    b_hold = [o for o in rows_b if o.split == "holdout"]
    embedder = build_embedder(embedder_kind, settings.embedding_model, cache_dir=str(settings.hf_home))
    featurizer = Featurizer(embedder, load_lexicons(settings.lexicon_dir, settings.stage_rules_path),
                            load_glossary(glossary_path), cache_dir)
    observed = [o.observed_query_sim for o in b_dev if o.observed_query_sim is not None]
    featurizer.cluster_query_impute = float(statistics.median(observed)) if observed else None
    projection = projection if projection is not None else featurizer.fit_projection(a_train)
    X = {name: featurizer.matrix(rows, projection)
         for name, rows in (("a_train", a_train), ("a_dev", a_dev), ("a_test", a_test), ("b_dev", b_dev),
                            ("b_hold", b_hold))}
    featurizer.save_cache()
    meta = {
        "a_file": str(a_path), "a_sha256": sha256_file(a_path), "b_file": str(b_path), "b_sha256": sha256_file(b_path),
        "glossary_sha256": sha256_file(glossary_path), "embedding_model": featurizer.embedding_model, "seed": seed,
        "a_origins": {"organizers": "gpb_dataset_2026_09 (только позитивы)",
                      "auxiliary_negatives": "team_negatives_v1 (разметка команды, один аннотатор)"},
        "a_train_ids": [o.sample_id for o in a_train], "a_dev_ids": [o.sample_id for o in a_dev],
        "a_test_ids": [o.sample_id for o in a_test], "a_excluded_linked_to_b": sorted(linked),
        "b_dev_ids": [o.sample_id for o in b_dev], "b_holdout_ids": [o.sample_id for o in b_hold],
        "cluster_query_impute_median_dev": featurizer.cluster_query_impute,
        "counts": {"a_train": _counts(a_train), "a_dev": _counts(a_dev), "a_test": _counts(a_test),
                   "b_dev": _counts(b_dev), "b_holdout": _counts(b_hold)},
        "cross_links": [list(link) for link in links],
        "projection": {"fitted_on": "A-train titles", "components": int(projection.components.shape[0])},
    }
    meta["a_train_sha256"] = hashlib.sha256("|".join(meta["a_train_ids"]).encode()).hexdigest()
    return Context(a_train, a_dev, a_test, b_dev, b_hold, links, featurizer, X, meta, projection)


def _counts(rows: list[Obs]) -> dict[str, int]:
    return {"rows": len(rows), "positives": sum(o.label for o in rows), "groups": len({o.group_id for o in rows}),
            "topics": len({o.topic for o in rows if o.topic})}


def _y(rows: list[Obs]) -> np.ndarray:
    return np.asarray([o.label for o in rows], dtype=float)


class Runner:
    """Выполняет протокол и пишет артефакты в `out`."""

    def __init__(self, ctx: Context, seed: int, out: Path, quick: bool) -> None:
        self.ctx, self.seed, self.out, self.quick = ctx, seed, out, quick
        self.boot = 200 if quick else 1000
        self.y = {k: _y(getattr(ctx, k)) for k in ("a_train", "a_dev", "a_test", "b_dev", "b_hold")}
        self.topics_dev = [o.topic for o in ctx.b_dev]
        self.folds = sorted({o.split for o in ctx.b_dev})
        self.a_ids = [o.sample_id for o in ctx.a_train]

    def log(self, message: str) -> None:
        with (self.out / "progress.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {message}\n")
        print(message, flush=True)

    def stage_a(self, X_train: np.ndarray | None = None) -> tuple[Checkpoint, str]:
        ctx = self.ctx
        X_train = ctx.X["a_train"] if X_train is None else X_train
        ck = train_stage_a(X_train, self.y["a_train"], [o.group_id for o in ctx.a_train], LAMBDAS_A, self.seed,
                           ctx.projection,
                           ctx.featurizer.embedding_model, ctx.featurizer.glossary,
                           {k: ctx.meta[k] for k in ("a_file", "a_sha256", "a_train_ids", "a_train_sha256",
                                                     "a_excluded_linked_to_b", "a_origins")})
        return ck, hashlib.sha256(json.dumps(ck.to_json(), ensure_ascii=False, indent=1).encode()).hexdigest()

    def cv(self, variant: str, params: dict[str, float], parent: tuple[Checkpoint, str],
           X_b: np.ndarray | None = None, X_a: np.ndarray | None = None, train_mask=None, flip_seed=None) -> np.ndarray:  # noqa: ANN001
        X_b = self.ctx.X["b_dev"] if X_b is None else X_b
        X_a = self.ctx.X["a_train"] if X_a is None else X_a
        y = self.y["b_dev"]
        oof = np.zeros(len(y))
        for fold in self.folds:
            valid = np.array([o.split == fold for o in self.ctx.b_dev])
            train = ~valid if train_mask is None else (~valid & train_mask)
            y_train = y[train].copy()
            if flip_seed is not None:
                rng = np.random.default_rng(flip_seed)
                flip = rng.random(len(y_train)) < 0.10
                y_train[flip] = 1.0 - y_train[flip]
            if y_train.sum() == 0:
                continue
            ck = train_stage_b(variant, params, parent[0], parent[1], X_b[train], y_train, X_a, self.y["a_train"],
                               self.a_ids, {"b_train_sha256": _ids_sha(self.ctx.b_dev, train)})
            oof[valid] = ck.logits(X_b[valid])
        return oof

    def run(self) -> dict[str, Any]:
        ctx = self.ctx
        started = time.time()
        stage_a, sha_a = self.stage_a()
        stage_a.metrics = {"a_dev": _a_metrics(self.y["a_dev"], stage_a.logits(ctx.X["a_dev"]))}
        sha_a = stage_a.save(self.out / "checkpoints" / "stage_a.json")
        parent = (stage_a, sha_a)
        self.log(f"stage A: l2={stage_a.params['l2']} A-dev ROC-AUC={stage_a.metrics['a_dev']['roc_auc']}")
        cv_rows, best = [], {}
        for variant in VARIANTS:
            grid = GRID[variant][:1] if self.quick and variant != "seq_laplace" else GRID[variant]
            for params in grid:
                oof = self.cv(variant, params, parent)
                row = {"variant": variant, "params": json.dumps(params), **M.summary(self.y["b_dev"], oof, self.topics_dev)}
                cv_rows.append(row)
                if variant not in best or (row["pr_auc"] or 0) > (best[variant]["summary"]["pr_auc"] or 0):
                    best[variant] = {"params": params, "oof": oof, "summary": row}
            self.log(f"cv {variant}: PR-AUC={best[variant]['summary']['pr_auc']:.3f} params={best[variant]['params']}")
        oof_a = stage_a.logits(ctx.X["b_dev"])
        best["stage_a"] = {"params": {}, "oof": oof_a, "summary": M.summary(self.y["b_dev"], oof_a, self.topics_dev)}
        forgetting = {"stage_a": stage_a.metrics["a_dev"]}
        full_models: dict[str, Checkpoint] = {"stage_a": stage_a}
        for variant in VARIANTS:
            ck = train_stage_b(variant, best[variant]["params"], stage_a, sha_a, ctx.X["b_dev"], self.y["b_dev"],
                               ctx.X["a_train"], self.y["a_train"], self.a_ids,
                               {"b_train_sha256": _ids_sha(ctx.b_dev, None), "b_train": "all dev folds"})
            full_models[variant] = ck
            forgetting[variant] = _a_metrics(self.y["a_dev"], ck.logits(ctx.X["a_dev"]))
        base_auc = forgetting["stage_a"]["roc_auc"] or 0.0
        eligible = [v for v in VARIANTS if (forgetting[v]["roc_auc"] or 0.0) >= base_auc - FORGETTING_TOLERANCE]
        pool = eligible or list(VARIANTS)
        chosen = max(pool, key=lambda v: (best[v]["summary"]["pr_auc"] or 0.0, v in PROTECTED))
        seq_pool = [v for v in pool if v.startswith("seq_")] or [v for v in VARIANTS if v.startswith("seq_")]
        recommended_seq = max(seq_pool, key=lambda v: (best[v]["summary"]["pr_auc"] or 0.0, v in PROTECTED))
        self.log(f"selected on dev: {chosen}; sequential recommendation: {recommended_seq}")
        baselines = self._baselines()
        baselines["stage_a_plus_emb_sim_query_equal_z"] = self._zero_shot_hybrid(best["stage_a"]["oof"])
        acceptance_dev = self._paired_dev(best[chosen]["oof"])
        ablations = self._ablations(recommended_seq, best[recommended_seq]["params"], parent)
        curve = self._learning_curve(recommended_seq, best[recommended_seq]["params"], parent)
        noise = self._noise(recommended_seq, best[recommended_seq]["params"], parent)
        calibration = {}
        for name, entry in best.items():
            cal = M.platt(entry["oof"], self.y["b_dev"])
            probs = M.calibrated(entry["oof"], cal)
            threshold = M.best_f1_threshold(probs, self.y["b_dev"])
            calibration[name] = {"platt": cal, "threshold": threshold, "dev_ece_5bins": M.ece(self.y["b_dev"], probs),
                                 "dev_brier": M.brier(self.y["b_dev"], probs),
                                 "dev_at_threshold": M.at_threshold(self.y["b_dev"], probs, threshold),
                                 "note": "калибровка и порог выбраны на тех же dev-OOF; независимая проверка — holdout"}
            if name in full_models:
                full_models[name].calibration, full_models[name].threshold = cal, threshold
        final = self._final(full_models, calibration, chosen)
        acceptance = self._acceptance(chosen, acceptance_dev, forgetting, final, ctx.featurizer.embedding_model)
        full_models[chosen].metrics["acceptance"] = acceptance
        for name, ck in full_models.items():
            if name != "stage_a":
                ck.metrics = {**ck.metrics, "dev_oof": best[name]["summary"], "a_dev": forgetting[name],
                              "holdout": final["systems"][name]["b_holdout"], "a_test": final["systems"][name]["a_test"]}
                ck.save(self.out / "checkpoints" / f"stage_b_{name}.json")
        length_index = FEATURE_NAMES_V2.index("spec_title_length")
        style = {"a_dev_roc_auc_title_length_only": M.roc_auc(self.y["a_dev"], ctx.X["a_dev"][:, length_index]),
                 "a_test_roc_auc_title_length_only": M.roc_auc(self.y["a_test"], ctx.X["a_test"][:, length_index]),
                 "note": "названия позитивов писали методологи, негативов — команда: длина названия сама разделяет "
                         "классы; метрики A отражают и авторский стиль"}
        report = {
            "a_style_diagnostic": style,
            "run": self.out.name, "duration_seconds": round(time.time() - started, 1), "quick": self.quick,
            "primary_metric": "dev pooled OOF PR-AUC (темы dev-фолдов)", "forgetting_tolerance": FORGETTING_TOLERANCE,
            "data": ctx.meta, "stage_a": {"params": stage_a.params, "a_dev": stage_a.metrics["a_dev"]},
            "cv_best": {k: {**v["summary"], "params": v["params"]} for k, v in best.items()},
            "dev_topic_bootstrap_pr_auc": {k: M.bootstrap_ci(self.y["b_dev"], v["oof"], self.topics_dev, M.pr_auc,
                                                             self.boot, self.seed) for k, v in best.items()},
            "forgetting_a_dev": forgetting, "chosen_on_dev": chosen, "recommended_sequential": recommended_seq,
            "baselines_dev": baselines, "ablations_dev": ablations, "learning_curve_dev": curve, "label_noise_dev": noise,
            "calibration": calibration, "final": final, "acceptance": acceptance,
        }
        _write_csv(self.out / "cv_table.csv", cv_rows)
        _write_predictions(self.out / "predictions_dev_oof.csv", ctx.b_dev, {k: v["oof"] for k, v in best.items()})
        (self.out / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=_json),
                                               encoding="utf-8")
        return report

    def _baselines(self) -> dict[str, Any]:
        ctx, y, topics = self.ctx, self.y["b_dev"], self.topics_dev
        X = ctx.X["b_dev"]
        result = {}
        for name, column in (("relevance_evidence_mean", "rel_query_evidence_mean"),
                             ("cluster_query_v1_feature", "rel_cluster_query_v1")):
            result[name] = M.summary(y, X[:, FEATURE_NAMES_V2.index(column)], topics)
        for name, attr in (("production_score", "observed_score"), ("production_emb_sim_query", "observed_query_sim")):
            idx = [i for i, o in enumerate(ctx.b_dev) if getattr(o, attr) is not None]
            scores = np.asarray([getattr(ctx.b_dev[i], attr) for i in idx])
            result[name] = {**M.summary(y[idx], scores, [topics[i] for i in idx]),
                            "note": "только живые строки со значением из журнала прогона"}
        return result

    def _live_dev(self) -> list[int]:
        return [i for i, o in enumerate(self.ctx.b_dev) if o.observed_score is not None]

    def _paired_dev(self, scores: np.ndarray) -> dict[str, Any]:
        live = self._live_dev()
        y = self.y["b_dev"][live]
        prod = np.asarray([self.ctx.b_dev[i].observed_score for i in live])
        topics = [self.topics_dev[i] for i in live]
        return {"rows": len(live), "pr_auc": M.paired_delta(y, scores[live], prod, topics, M.pr_auc, self.boot, self.seed),
                "roc_auc": M.paired_delta(y, scores[live], prod, topics, M.roc_auc, self.boot, self.seed)}

    def _zero_shot_hybrid(self, stage_a_scores: np.ndarray) -> dict[str, Any]:
        live = self._live_dev()
        a = stage_a_scores[live]
        q = np.asarray([self.ctx.b_dev[i].observed_query_sim or 0.0 for i in live])
        z = (a - a.mean()) / (a.std() or 1.0) + (q - q.mean()) / (q.std() or 1.0)
        return {**M.summary(self.y["b_dev"][live], z, [self.topics_dev[i] for i in live]),
                "note": "без обучения на B: равные веса z-оценок Stage A и e5-близости кластера к запросу из журнала"}

    def _acceptance(self, chosen: str, dev: dict[str, Any], forgetting: dict[str, Any], final: dict[str, Any],
                    embedder: str) -> dict[str, Any]:
        holdout = final["paired_vs_production"].get(f"{chosen} − production_score", {}).get("pr_auc", {})
        checks = {
            "dev_pr_auc_gain_ci_low_gt_0": bool((dev["pr_auc"].get("ci_low") or -1.0) > 0.0),
            "forgetting_within_tolerance": bool((forgetting[chosen]["roc_auc"] or 0.0)
                                                >= (forgetting["stage_a"]["roc_auc"] or 0.0) - FORGETTING_TOLERANCE),
            "holdout_pr_auc_not_worse_than_0_05": bool(holdout.get("delta") is not None and holdout["delta"] >= -0.05),
            "production_embedder": embedder.startswith("intfloat/"),
        }
        return {"rules": "заданы до финальной оценки (docs/ml_rework/PROTOCOL.md)", "checks": checks,
                "dev_vs_production": dev, "holdout_vs_production_pr_auc": holdout,
                "decision": "accept" if all(checks.values()) else "reject_keep_current_model"}

    def _ablations(self, variant: str, params: dict[str, float], parent: tuple[Checkpoint, str]) -> list[dict[str, Any]]:
        ctx, y, topics = self.ctx, self.y["b_dev"], self.topics_dev
        rows = [{"ablation": "full", **M.summary(y, self.cv(variant, params, parent), topics)}]
        drops = [(g, [i for i, n in enumerate(FEATURE_NAMES_V2) if FEATURE_GROUPS_V2[n] == g]) for g in ABLATION_GROUPS]
        drops.append(("rel_cluster_query_v1 (зависит от LLM-расширения запроса)",
                      [FEATURE_NAMES_V2.index("rel_cluster_query_v1")]))
        for name, cols in drops:
            X_a, X_b = ctx.X["a_train"].copy(), ctx.X["b_dev"].copy()
            X_a[:, cols], X_b[:, cols] = 0.0, 0.0
            ck_a, sha = self.stage_a(X_a)
            rows.append({"ablation": f"without {name}",
                         **M.summary(y, self.cv(variant, params, (ck_a, sha), X_b=X_b, X_a=X_a), topics)})
        rows.append({"ablation": "ML score = constant", **M.summary(y, np.zeros(len(y)), topics)})
        rng = np.random.default_rng(self.seed)
        oof = self.cv(variant, params, parent)
        perm = []
        for _ in range(50 if self.quick else 200):
            shuffled = oof.copy()
            for topic in set(topics):
                idx = [i for i, t in enumerate(topics) if t == topic]
                shuffled[idx] = shuffled[rng.permutation(idx)]
            perm.append(M.pr_auc(y, shuffled))
        rows.append({"ablation": "ML score permuted within topic (mean)", "pr_auc": float(np.mean(perm)),
                     "n": int(len(y)), "positives": int(y.sum())})
        return rows

    def _learning_curve(self, variant: str, params: dict[str, float], parent: tuple[Checkpoint, str]) -> list[dict]:
        rows = []
        groups = sorted({o.group_id for o in self.ctx.b_dev})
        for fraction in (0.25, 0.5, 0.75, 1.0):
            for seed in range(2 if self.quick else 5):
                rng = np.random.default_rng(self.seed + seed)
                keep = set(rng.choice(groups, max(2, int(round(len(groups) * fraction))), replace=False))
                mask = np.array([o.group_id in keep for o in self.ctx.b_dev])
                for name, (v, p) in (("candidate", (variant, params)), ("b_only", ("b_only", {"l2": 3.0}))):
                    oof = self.cv(v, p, parent, train_mask=mask)
                    rows.append({"fraction": fraction, "seed": seed, "system": name, "variant": v,
                                 "pr_auc": M.pr_auc(self.y["b_dev"], oof), "roc_auc": M.roc_auc(self.y["b_dev"], oof)})
        return rows

    def _noise(self, variant: str, params: dict[str, float], parent: tuple[Checkpoint, str]) -> list[dict]:
        rows = []
        for seed in range(2 if self.quick else 5):
            for name, (v, p) in (("candidate", (variant, params)), ("b_only", ("b_only", {"l2": 3.0}))):
                oof = self.cv(v, p, parent, flip_seed=self.seed + 100 + seed)
                rows.append({"flip_rate": 0.10, "seed": seed, "system": name, "pr_auc": M.pr_auc(self.y["b_dev"], oof)})
        return rows

    def _final(self, models: dict[str, Checkpoint], calibration: dict[str, Any], chosen: str) -> dict[str, Any]:
        ctx = self.ctx
        y_h, topics_h = self.y["b_hold"], [o.topic for o in ctx.b_hold]
        groups_h = [o.group_id for o in ctx.b_hold]
        systems, preds = {}, {}
        for name, ck in models.items():
            raw = ck.logits(ctx.X["b_hold"])
            probs = M.calibrated(raw, calibration[name]["platt"])
            preds[name] = probs
            systems[name] = {
                "b_holdout": {**M.summary(y_h, probs, topics_h), "brier": M.brier(y_h, probs),
                              "ece_5bins": M.ece(y_h, probs),
                              "at_dev_threshold": M.at_threshold(y_h, probs, calibration[name]["threshold"]),
                              "pr_auc_ci_rows": M.bootstrap_ci(y_h, probs, groups_h, M.pr_auc, self.boot, self.seed)},
                "a_test": _a_metrics(self.y["a_test"], ck.logits(ctx.X["a_test"])),
            }
        live = [i for i, o in enumerate(ctx.b_hold) if o.observed_score is not None]
        prod = np.asarray([ctx.b_hold[i].observed_score for i in live])
        qsim = np.asarray([ctx.b_hold[i].observed_query_sim or 0.0 for i in live])
        y_live, g_live, t_live = y_h[live], [groups_h[i] for i in live], [topics_h[i] for i in live]
        systems["production_score"] = {"b_holdout": M.summary(y_live, prod, t_live), "a_test": None}
        systems["production_emb_sim_query"] = {"b_holdout": M.summary(y_live, qsim, t_live), "a_test": None}
        paired = {}
        for name in (chosen, "stage_a", "b_only", "seq_laplace", "seq_laplace_replay"):
            cand = preds[name][live]
            paired[f"{name} − production_score"] = {
                "pr_auc": M.paired_delta(y_live, cand, prod, g_live, M.pr_auc, self.boot, self.seed),
                "roc_auc": M.paired_delta(y_live, cand, prod, g_live, M.roc_auc, self.boot, self.seed)}
        _write_predictions(self.out / "predictions_holdout.csv", ctx.b_hold, preds)
        usage = {"run": self.out.name, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                 "holdout_ids_sha256": _ids_sha(ctx.b_hold, None), "systems": sorted(systems)}
        with (self.out.parent / "holdout_usage.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(usage, ensure_ascii=False) + "\n")
        with (self.out.parent / "holdout_usage.jsonl").open(encoding="utf-8") as handle:
            uses = sum(1 for line in handle if line.strip()
                       and json.loads(line).get("holdout_ids_sha256") == usage["holdout_ids_sha256"])
        return {"systems": systems, "paired_vs_production": paired, "holdout_live_rows": len(live),
                "holdout_uses_including_this_run": uses,
                "note": f"holdout — тем: {len(set(topics_h))}; ДИ по строкам (группам), не по темам; "
                        "вычисления считаются по тому же набору строк holdout (holdout_ids_sha256)"}


def _a_metrics(y: np.ndarray, logits: np.ndarray) -> dict[str, Any]:
    return {"n": int(len(y)), "positives": int(y.sum()), "roc_auc": M.roc_auc(y, logits), "pr_auc": M.pr_auc(y, logits),
            "accuracy_at_logit0": float(np.mean((logits >= 0).astype(int) == y))}


def _ids_sha(rows: list[Obs], mask) -> str:  # noqa: ANN001
    ids = [o.sample_id for i, o in enumerate(rows) if mask is None or mask[i]]
    return hashlib.sha256("|".join(ids).encode()).hexdigest()


def _json(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(type(value))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _write_predictions(path: Path, rows: list[Obs], scores: dict[str, np.ndarray]) -> None:
    names = sorted(scores)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sample_id", "split", "topic", "label", "label_kind", "observed_production_score", *names])
        for i, o in enumerate(rows):
            writer.writerow([o.sample_id, o.split, o.topic, o.label, o.label_kind, o.observed_score,
                             *[round(float(scores[n][i]), 6) for n in names]])


def run_experiments(settings, embedder_kind: str, seed: int, run_id: str | None, quick: bool) -> Path:  # noqa: ANN001
    """Точка входа: новый каталог прогона (существующий не перезаписывается)."""
    root = settings.reports_dir / "experiments"
    run_id = run_id or f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{embedder_kind}{'-quick' if quick else ''}"
    out = root / run_id
    out.mkdir(parents=True, exist_ok=False)
    ctx = build_context(settings, embedder_kind, seed, root / "cache")
    (out / "data_manifest.json").write_text(json.dumps(ctx.meta, ensure_ascii=False, indent=1), encoding="utf-8")
    Runner(ctx, seed, out, quick).run()
    manifest = {p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(out.rglob("*")) if p.is_file()}
    (out / "run_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return out
