"""Логистическая регрессия для последовательного обучения: L-BFGS, точный гессиан, квадратичный якорь.

Якорь (θ_A, P) реализует штрафы L2-SP (P = λI) и лапласовское/EWC-приближение апостериорного
распределения Stage A (P = α·H_A, H_A — гессиан целевой функции Stage A в точке θ_A).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import minimize


@dataclass(frozen=True, slots=True)
class Block:
    """Слагаемое данных: преобразованные признаки, метки, веса строк."""

    X: np.ndarray
    y: np.ndarray
    w: np.ndarray


@dataclass(frozen=True, slots=True)
class Anchor:
    """Квадратичный штраф ½(θ − θ₀)ᵀP(θ − θ₀)."""

    theta: np.ndarray
    precision: np.ndarray


def augment(X: np.ndarray) -> np.ndarray:
    """Столбец свободного члена."""
    return np.hstack([X, np.ones((X.shape[0], 1))])


def balanced_weights(y: np.ndarray) -> np.ndarray:
    """Веса, уравнивающие вклад классов; сумма = числу строк."""
    y = np.asarray(y)
    n, pos = len(y), int(y.sum())
    neg = n - pos
    return np.where(y == 1, n / (2.0 * max(pos, 1)), n / (2.0 * max(neg, 1))).astype(np.float64)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * z))


def objective(theta: np.ndarray, blocks: list[Block], l2: float, anchor: Anchor | None) -> tuple[float, np.ndarray]:
    """Взвешенная логистическая потеря + L2 (без свободного члена) + якорь; значение и градиент."""
    value = 0.0
    grad = np.zeros_like(theta)
    for block in blocks:
        if block.X.shape[0] == 0:
            continue
        xa = augment(block.X)
        z = xa @ theta
        value += float(np.sum(block.w * (np.logaddexp(0.0, z) - block.y * z)))
        grad += xa.T @ (block.w * (_sigmoid(z) - block.y))
    reg = theta.copy()
    reg[-1] = 0.0
    value += 0.5 * l2 * float(reg @ reg)
    grad += l2 * reg
    if anchor is not None:
        diff = theta - anchor.theta
        pdiff = anchor.precision @ diff
        value += 0.5 * float(diff @ pdiff)
        grad += pdiff
    return value, grad


def fit(
    blocks: list[Block], l2: float, anchor: Anchor | None = None, init: np.ndarray | None = None,
    maxiter: int = 2000,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Минимизация L-BFGS-B; init — перенос состояния (иначе нули)."""
    dim = blocks[0].X.shape[1] + 1
    theta0 = np.zeros(dim) if init is None else np.asarray(init, dtype=np.float64).copy()
    result = minimize(objective, theta0, args=(blocks, l2, anchor), jac=True, method="L-BFGS-B",
                      options={"maxiter": int(maxiter), "gtol": 1e-7})
    return result.x, {"iterations": int(result.nit), "converged": bool(result.success),
                      "objective": float(result.fun), "started_from": "zeros" if init is None else "parent"}


def hessian(theta: np.ndarray, blocks: list[Block], l2: float) -> np.ndarray:
    """Гессиан потери + L2 в точке θ (для логистической модели совпадает с информацией Фишера)."""
    dim = theta.size
    matrix = np.zeros((dim, dim))
    for block in blocks:
        if block.X.shape[0] == 0:
            continue
        xa = augment(block.X)
        p = _sigmoid(xa @ theta)
        matrix += (xa * (block.w * p * (1.0 - p))[:, None]).T @ xa
    reg = np.full(dim, l2)
    reg[-1] = 0.0
    return matrix + np.diag(reg)


def logits(theta: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Логит модели."""
    return augment(X) @ theta
