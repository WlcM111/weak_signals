"""Stage A и Stage B: замороженные преобразования, перенос состояния, защита знаний A, происхождение."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from analyzer.domain.features_v2 import FEATURE_NAMES_V2, TOPIC_DEPENDENT, Projection

from ml.seq import linear

TOPIC_INDEX = FEATURE_NAMES_V2.index("has_topic")
B_MASK = np.array([name in TOPIC_DEPENDENT for name in FEATURE_NAMES_V2])
VARIANTS = ("b_only", "seq_finetune_early", "seq_finetune", "seq_l2sp", "seq_laplace", "seq_laplace_replay", "joint")
PROTECTED = {"seq_l2sp", "seq_laplace", "seq_laplace_replay"}
REPLAY = {"seq_laplace_replay", "joint"}


@dataclass(slots=True)
class Transform:
    """Стандартизация: признаки A — по A-train (заморожено на Stage A); признаки темы — по B-train."""

    mu: np.ndarray
    sigma: np.ndarray

    def apply(self, X: np.ndarray) -> np.ndarray:
        """Преобразование; признаки темы при has_topic = 0 равны 0 (не определены для описаний A)."""
        Z = (np.asarray(X, dtype=np.float64) - self.mu) / self.sigma
        Z[:, B_MASK] *= X[:, [TOPIC_INDEX]]
        return Z

    def fold(self, theta: np.ndarray) -> tuple[np.ndarray, float]:
        """Веса в пространстве исходных признаков (для JSON-артефакта analyzer)."""
        w, b = theta[:-1], float(theta[-1])
        raw = w / self.sigma
        a_mask = ~B_MASK
        intercept = b - float(np.sum(w[a_mask] * self.mu[a_mask] / self.sigma[a_mask]))
        raw = raw.copy()
        raw[TOPIC_INDEX] -= float(np.sum(w[B_MASK] * self.mu[B_MASK] / self.sigma[B_MASK]))
        return raw, intercept

    def to_json(self) -> dict[str, list[float]]:
        return {"mu": self.mu.tolist(), "sigma": self.sigma.tolist()}

    @staticmethod
    def from_json(payload: dict[str, list[float]]) -> Transform:
        return Transform(np.asarray(payload["mu"], dtype=np.float64), np.asarray(payload["sigma"], dtype=np.float64))


def transform_a(X: np.ndarray) -> Transform:
    """Статистики признаков по A-train; признаки темы — заглушки (для A они всегда 0)."""
    mu, sd = X.mean(axis=0), X.std(axis=0)
    mu[B_MASK], sd[B_MASK] = 0.0, 1.0
    mu[TOPIC_INDEX], sd[TOPIC_INDEX] = 0.0, 1.0
    sd[sd < 1e-9] = 1.0
    return Transform(mu, sd)


def extend_b(parent: Transform, X_b: np.ndarray) -> Transform:
    """Добавляет статистики признаков темы по B-train; статистики признаков A не меняются."""
    rows = X_b[X_b[:, TOPIC_INDEX] > 0.5]
    mu, sd = parent.mu.copy(), parent.sigma.copy()
    if rows.shape[0]:
        mb, sb = rows.mean(axis=0), rows.std(axis=0)
        sb[sb < 1e-9] = 1.0
        mu[B_MASK], sd[B_MASK] = mb[B_MASK], sb[B_MASK]
    return Transform(mu, sd)


@dataclass(slots=True)
class Checkpoint:
    """Контрольная точка этапа с происхождением и всем необходимым для экспорта."""

    stage: str
    checkpoint_id: str
    parent_id: str | None
    parent_sha256: str | None
    variant: str
    theta: np.ndarray
    transform: Transform
    projection: Projection
    embedding_model: str
    glossary: dict[str, str]
    l2_a: float
    hessian_a: np.ndarray | None
    params: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    calibration: dict[str, float] = field(default_factory=lambda: {"slope": 1.0, "offset": 0.0})
    threshold: float = 0.5
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def logits(self, X_raw: np.ndarray) -> np.ndarray:
        """Некалиброванный логит для исходных признаков."""
        return linear.logits(self.theta, self.transform.apply(X_raw))

    def probability(self, X_raw: np.ndarray) -> np.ndarray:
        """Калиброванная вероятность."""
        z = self.calibration["slope"] * self.logits(X_raw) + self.calibration["offset"]
        return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "ws-seq-checkpoint-v1", "stage": self.stage, "checkpoint_id": self.checkpoint_id,
            "parent_id": self.parent_id, "parent_sha256": self.parent_sha256, "variant": self.variant,
            "feature_names": list(FEATURE_NAMES_V2), "theta": self.theta.tolist(),
            "transform": self.transform.to_json(), "projection": self.projection.to_json(),
            "embedding_model": self.embedding_model, "glossary": self.glossary, "l2_a": self.l2_a,
            "hessian_a": None if self.hessian_a is None else self.hessian_a.tolist(), "params": self.params,
            "data": self.data, "metrics": self.metrics, "calibration": self.calibration,
            "threshold": self.threshold, "created_at": self.created_at,
        }

    def save(self, path: Path) -> str:
        """Пишет JSON и возвращает его sha256."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_json(), ensure_ascii=False, indent=1)
        path.write_text(text, encoding="utf-8")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_checkpoint(path: Path) -> tuple[Checkpoint, str]:
    """Читает контрольную точку и проверяет совместимость пространства признаков."""
    text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(text)
    if payload.get("format") != "ws-seq-checkpoint-v1" or tuple(payload["feature_names"]) != FEATURE_NAMES_V2:
        raise ValueError("контрольная точка несовместима с признаками v2 текущего кода")
    checkpoint = Checkpoint(
        stage=payload["stage"], checkpoint_id=payload["checkpoint_id"], parent_id=payload["parent_id"],
        parent_sha256=payload["parent_sha256"], variant=payload["variant"],
        theta=np.asarray(payload["theta"], dtype=np.float64), transform=Transform.from_json(payload["transform"]),
        projection=Projection.from_json(payload["projection"]), embedding_model=payload["embedding_model"],
        glossary=dict(payload["glossary"]), l2_a=float(payload["l2_a"]),
        hessian_a=None if payload["hessian_a"] is None else np.asarray(payload["hessian_a"], dtype=np.float64),
        params=payload["params"], data=payload["data"], metrics=payload["metrics"],
        calibration=payload["calibration"], threshold=float(payload["threshold"]), created_at=payload["created_at"],
    )
    return checkpoint, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _id(stage: str, *parts: Any) -> str:
    return f"ckpt-{stage}-" + hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:12]


def group_folds(groups: list[str], y: np.ndarray, k: int, seed: int) -> list[tuple[np.ndarray, np.ndarray]]:
    """Стратифицированные групповые фолды (группа целиком в одном фолде)."""
    by_group: dict[str, list[int]] = {}
    for index, group in enumerate(groups):
        by_group.setdefault(group, []).append(index)
    assignment: dict[str, int] = {}
    for label in (0, 1):
        names = sorted((g for g, idx in by_group.items() if int(round(np.mean(y[idx]))) == label),
                       key=lambda g: hashlib.sha256(f"{seed}|{g}".encode()).hexdigest())
        for position, name in enumerate(names):
            assignment[name] = position % k
    fold_of = np.array([assignment[g] for g in groups])
    return [(np.where(fold_of != f)[0], np.where(fold_of == f)[0]) for f in range(k)]


def train_stage_a(
    X: np.ndarray, y: np.ndarray, groups: list[str], lambdas: tuple[float, ...], seed: int,
    projection: Projection, embedding_model: str, glossary: dict[str, str], data: dict[str, Any],
) -> Checkpoint:
    """Stage A: выбор λ групповой CV на A-train, обучение, гессиан для последующего переноса."""
    from ml.seq.metrics import roc_auc  # noqa: PLC0415

    cv: dict[str, float | None] = {}
    folds = group_folds(groups, y, 5, seed)
    for lam in lambdas:
        oof = np.zeros(len(y))
        for train, valid in folds:
            t = transform_a(X[train])
            theta, _ = linear.fit([linear.Block(t.apply(X[train]), y[train], linear.balanced_weights(y[train]))], lam)
            oof[valid] = linear.logits(theta, t.apply(X[valid]))
        cv[str(lam)] = roc_auc(y, oof)
    best = max(lambdas, key=lambda lam: ((cv[str(lam)] or 0.0), lam))
    t = transform_a(X)
    block = linear.Block(t.apply(X), y, linear.balanced_weights(y))
    theta, info = linear.fit([block], best)
    hess = linear.hessian(theta, [block], best)
    return Checkpoint(
        stage="A", checkpoint_id=_id("A", data.get("a_train_sha256"), best, seed, embedding_model),
        parent_id=None, parent_sha256=None, variant="stage_a", theta=theta, transform=t, projection=projection,
        embedding_model=embedding_model, glossary=glossary, l2_a=best, hessian_a=hess,
        params={"l2": best, "cv_roc_auc_by_l2": cv, **info}, data=data,
    )


def train_stage_b(
    variant: str, params: dict[str, float], parent: Checkpoint, parent_sha256: str,
    X_b: np.ndarray, y_b: np.ndarray, X_a: np.ndarray, y_a: np.ndarray, a_ids: list[str], data: dict[str, Any],
) -> Checkpoint:
    """Stage B из состояния Stage A; replay использует только переданные строки A-train."""
    if variant not in VARIANTS:
        raise ValueError(f"неизвестный вариант {variant}")
    t = extend_b(parent.transform, X_b)
    z_b, z_a = t.apply(X_b), t.apply(X_a)
    w_b, w_a0 = linear.balanced_weights(y_b), linear.balanced_weights(y_a)
    w_a = w_a0 * (w_b.sum() / max(w_a0.sum(), 1e-12))
    block_b = linear.Block(z_b, y_b, w_b)
    theta_a = parent.theta
    dim = theta_a.size
    h_a = linear.hessian(theta_a, [linear.Block(z_a, y_a, w_a0)], parent.l2_a)
    eye = np.eye(dim)
    if variant == "b_only":
        theta, info = linear.fit([block_b], params["l2"])
    elif variant == "seq_finetune_early":
        theta, info = linear.fit([block_b], params["l2"], init=theta_a, maxiter=int(params.get("maxiter", 5)))
    elif variant == "seq_finetune":
        theta, info = linear.fit([block_b], params["l2"], init=theta_a)
    elif variant == "seq_l2sp":
        precision = params["l2sp"] * eye
        precision[-1, -1] = 1e-6
        theta, info = linear.fit([block_b], 0.0, linear.Anchor(theta_a, precision), init=theta_a)
    elif variant == "seq_laplace":
        theta, info = linear.fit([block_b], 0.0, linear.Anchor(theta_a, params["alpha"] * h_a + 1e-6 * eye), init=theta_a)
    elif variant == "seq_laplace_replay":
        replay = linear.Block(z_a, y_a, params["rho"] * w_a)
        theta, info = linear.fit([block_b, replay], 0.0, linear.Anchor(theta_a, params["alpha"] * h_a + 1e-6 * eye),
                                 init=theta_a)
    else:
        theta, info = linear.fit([block_b, linear.Block(z_a, y_a, params["w_a"] * w_a)], params["l2"])
    record = {
        **{k: float(v) for k, v in params.items()}, **info,
        "distance_from_parent": float(np.linalg.norm(theta - theta_a)),
        "parent_hessian_recomputed_max_abs_diff": float(np.max(np.abs(h_a - parent.hessian_a)))
        if parent.hessian_a is not None else None,
        "uses_parent_state": variant not in {"b_only", "joint"},
        "replay_ids": list(a_ids) if variant in REPLAY else [],
        "replay_only_a_train": True,
    }
    return Checkpoint(
        stage="B", checkpoint_id=_id("B", parent.checkpoint_id, variant, sorted(params.items()), data.get("b_train_sha256")),
        parent_id=parent.checkpoint_id, parent_sha256=parent_sha256, variant=variant, theta=theta, transform=t,
        projection=parent.projection, embedding_model=parent.embedding_model, glossary=parent.glossary,
        l2_a=parent.l2_a, hessian_a=None, params=record, data=data,
    )
