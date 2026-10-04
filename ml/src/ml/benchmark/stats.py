"""Неопределённость метрик режима А: бутстрэп по темам, парное сравнение, каппа Коэна."""

from __future__ import annotations

import random
from collections.abc import Sequence

ITERATIONS = 2000
SEED = 20261003


def mean(values: Sequence[float]) -> float | None:
    """Среднее; None для пустого набора (метрика не определена)."""
    return sum(values) / len(values) if values else None


def bootstrap_ci(values: Sequence[float], iterations: int = ITERATIONS, seed: int = SEED) -> list[float] | None:
    """95 % перцентильный интервал среднего бутстрэпом по единицам (темам); None, если единиц меньше двух."""
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(iterations))
    return [round(means[int(0.025 * iterations)], 4), round(means[int(0.975 * iterations) - 1], 4)]


def paired_bootstrap(a: dict[str, float], b: dict[str, float], iterations: int = ITERATIONS,
                     seed: int = SEED) -> dict[str, object]:
    """Разность B − A на общих темах: среднее и 95 % интервал бутстрэпом по темам (парное сравнение)."""
    common = sorted(set(a) & set(b))
    diffs = [b[key] - a[key] for key in common]
    return {"topics": len(common), "mean_diff": round(mean(diffs), 4) if diffs else None,
            "ci95": bootstrap_ci(diffs, iterations, seed), "only_in_a": sorted(set(a) - set(b)),
            "only_in_b": sorted(set(b) - set(a))}


def cohen_kappa(a: Sequence[int], b: Sequence[int]) -> float | None:
    """Каппа Коэна для двух бинарных разметок; None без пар или при согласии, равном случайному (pe = 1)."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return round((po - pe) / (1 - pe), 4) if pe < 1 else None


def kappa_ci(a: Sequence[int], b: Sequence[int], iterations: int = ITERATIONS, seed: int = SEED) -> list[float] | None:
    """95 % интервал каппы бутстрэпом по парам; None, если пар меньше двух или каппа не определена."""
    pairs = list(zip(a, b, strict=True))
    if len(pairs) < 2:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(iterations):
        sample = [rng.choice(pairs) for _ in pairs]
        value = cohen_kappa([x for x, _ in sample], [y for _, y in sample])
        if value is not None:
            values.append(value)
    if len(values) < iterations // 2:
        return None
    values.sort()
    return [round(values[int(0.025 * len(values))], 4), round(values[int(0.975 * len(values)) - 1], 4)]
