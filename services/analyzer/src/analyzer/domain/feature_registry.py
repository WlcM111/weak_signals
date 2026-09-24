"""Реестр интерпретируемых признаков v1 (`schemas/feature_registry_v1.json`).

Порядок признаков в реестре = порядок вектора признаков; он общий для trainer и analyzer.
`emb_sim_query` не подаётся в модель (§12.7 ТЗ): модель обучается на 24 признаках, а этот признак
используется только правилом OFF_TOPIC и отдаётся клиенту с нулевым вкладом.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from analyzer.domain.errors import InvariantViolation
from analyzer.domain.values import FeatureDirection

EXCLUDED_FROM_MODEL = frozenset({"emb_sim_query"})
FEATURE_SCHEMA_VERSION = "v1"
EXPECTED_FEATURE_COUNT = 25
# Поддерживаемые реестры: v1 — 25 признаков, v2 — состав задаёт артефакт (analyzer.domain.features_v2).
SUPPORTED_SCHEMAS: dict[str, int | None] = {"v1": EXPECTED_FEATURE_COUNT, "v2": None}


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Описание одного признака реестра."""

    name: str
    group: str
    type: str
    low: float
    high: float
    label_ru: str
    expected_direction: FeatureDirection
    used_in_model: bool

    def clip(self, value: float) -> float:
        """Клиппинг значения по диапазону реестра; целочисленные признаки округляются."""
        clipped = min(self.high, max(self.low, float(value)))
        return float(round(clipped)) if self.type == "int" else clipped


@dataclass(frozen=True, slots=True)
class FeatureRegistry:
    """Упорядоченный набор признаков и производные срезы."""

    version: str
    features: tuple[FeatureSpec, ...]

    @staticmethod
    def from_mapping(payload: dict[str, Any]) -> FeatureRegistry:
        """Строит реестр из разобранного JSON; проверяет состав и уникальность имён."""
        version = str(payload.get("feature_schema_version", ""))
        if version not in SUPPORTED_SCHEMAS:
            raise InvariantViolation(f"поддерживается только реестр признаков {FEATURE_SCHEMA_VERSION} или v2")
        items = payload.get("features")
        expected = SUPPORTED_SCHEMAS[version]
        if not isinstance(items, list) or not items or (expected is not None and len(items) != expected):
            raise InvariantViolation(f"в реестре ожидается {expected or 'непустой список'} признаков")
        features: list[FeatureSpec] = []
        for item in items:
            low, high = item["range"]
            features.append(
                FeatureSpec(
                    name=item["name"],
                    group=item["group"],
                    type=item["type"],
                    low=float(low),
                    high=float(high),
                    label_ru=item["label_ru"],
                    expected_direction=FeatureDirection(item["expected_direction"]),
                    used_in_model=bool(item.get("used_in_model", item["name"] not in EXCLUDED_FROM_MODEL)),
                )
            )
        names = [feature.name for feature in features]
        if len(set(names)) != len(names):
            raise InvariantViolation("имена признаков в реестре не уникальны")
        return FeatureRegistry(version=version, features=tuple(features))

    @property
    def names(self) -> tuple[str, ...]:
        """Имена всех признаков в порядке реестра."""
        return tuple(feature.name for feature in self.features)

    @property
    def model_names(self) -> tuple[str, ...]:
        """Имена признаков, подаваемых в модель (без `emb_sim_query`)."""
        return tuple(feature.name for feature in self.features if feature.used_in_model)

    def spec(self, name: str) -> FeatureSpec:
        """Описание признака по имени."""
        for feature in self.features:
            if feature.name == name:
                return feature
        raise InvariantViolation(f"признак {name} отсутствует в реестре")

    def vector(self, values: dict[str, float]) -> tuple[float, ...]:
        """Значения в порядке реестра с клиппингом; отсутствующий признак — ошибка инварианта."""
        missing = set(self.names) - set(values)
        if missing:
            raise InvariantViolation(f"не вычислены признаки: {', '.join(sorted(missing))}")
        return tuple(feature.clip(values[feature.name]) for feature in self.features)

    def model_vector(self, values: dict[str, float]) -> tuple[float, ...]:
        """Значения признаков модели (24) в порядке реестра, с клиппингом."""
        return tuple(
            feature.clip(values[feature.name]) for feature in self.features if feature.used_in_model
        )
