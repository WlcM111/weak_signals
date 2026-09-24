"""Эквивалентность признаков обучения и инференса (§6.3 HANDOFF: один источник истины).

Тест проверяет, что `ml.features` не содержит собственных формул: на десяти фиксированных примерах
значения совпадают с тем, что вычисляет домен analyzer, вызванный напрямую.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from analyzer.domain.features import (
    EncyclopediaSignal,
    embedding_features,
    empty_collection_features,
    encyclopedia_features,
    lexical_features,
)

from ml.adapters.embedders import HashingEmbedder
from ml.dataset import TrainingRow
from ml.features import FeatureContext, build_matrix, centroids_from, row_features

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 16, tzinfo=UTC)
EXAMPLES = (
    ("Нейроморфные чипы для edge-инференса", "Прототип показан в лаборатории, пилотные испытания в 2026 году."),
    ("Контейнеризация приложений", "Стандарт OCI, массовое внедрение, рынок сформирован."),
    ("Революционная платформа без аналогов", "Первый в мире продукт, меняет всё."),
    ("Квантовые сенсоры в медицинской диагностике", "Посевной раунд $5 млн, доклинические испытания."),
    ("Открытый банкинг по API", "Регуляторное требование, повсеместное внедрение банками."),
    ("Agent identity для ИИ-агентов", "Компании вышли из stealth в 2026 году с Series A."),
    ("Промышленные экзоскелеты", "Серийные модели поставляют Ottobock и Comau."),
    ("Интернет вещей", "Широкая категория подключённых устройств."),
    ("Федеративное обучение на устройствах", "Модель обучается локально, в центр уходят веса."),
    ("Самовосстанавливающаяся кибербезопасность", "Обещание автоматического устранения последствий атак."),
)


def make_row(index: int, title: str, description: str) -> TrainingRow:
    """Строка набора для примера."""
    return TrainingRow(
        row_id=f"pos-{index:04d}",
        group_id=f"example-{index}",
        origin="gpb_dataset_2026_09",
        title=title,
        description=description,
        domain_tag="other",
        label=1,
        label_kind="WEAK_SIGNAL",
        source_urls=["https://example.org/1"],
        annotators=["methodologists_gpb"],
    )


class FeatureParityTest(unittest.TestCase):
    """Значения признаков обучения совпадают с доменом инференса."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.context = FeatureContext.load(
            ROOT / "schemas" / "feature_registry_v1.json",
            ROOT / "services" / "analyzer" / "config" / "lexicons",
            ROOT / "services" / "analyzer" / "config" / "stage_rules.yaml",
            NOW,
        )
        cls.embedder = HashingEmbedder()
        cls.rows = [make_row(index, title, text) for index, (title, text) in enumerate(EXAMPLES, start=1)]
        cls.vectors = cls.embedder.encode([row.text for row in cls.rows], "passage: ")
        cls.weak = cls.vectors[0]
        cls.mature = cls.vectors[1]

    def test_all_examples_match_analyzer_domain(self) -> None:
        for index, row in enumerate(self.rows):
            with self.subTest(title=row.title):
                produced = row_features(row, self.context, self.vectors[index], self.weak, self.mature)
                expected = {
                    **lexical_features(row.text, self.context.lexicons, NOW),
                    **empty_collection_features(),
                    **encyclopedia_features(EncyclopediaSignal(), NOW),
                    **embedding_features(self.vectors[index], self.weak, self.mature, None),
                    "emb_sim_query": 1.0,
                }
                self.assertEqual(set(produced), set(expected))
                for name, value in expected.items():
                    self.assertAlmostEqual(produced[name], value, places=10, msg=name)

    def test_registry_vector_width(self) -> None:
        values = [row_features(row, self.context, self.vectors[index], self.weak, self.mature)
                  for index, row in enumerate(self.rows)]
        self.assertEqual(build_matrix(values, self.context.registry).shape, (10, 24))
        self.assertEqual(build_matrix(values, self.context.registry, model_only=False).shape, (10, 25))

    def test_emb_sim_query_is_constant_at_training(self) -> None:
        values = row_features(self.rows[0], self.context, self.vectors[0], self.weak, self.mature)
        self.assertEqual(values["emb_sim_query"], 1.0)

    def test_centroids_are_unit_and_label_specific(self) -> None:
        labels = np.asarray([1, 0] * 5)
        weak, mature = centroids_from(self.vectors, labels)
        self.assertAlmostEqual(float(np.linalg.norm(weak)), 1.0, places=5)
        self.assertAlmostEqual(float(np.linalg.norm(mature)), 1.0, places=5)
        self.assertFalse(np.allclose(weak, mature))


class HashingEmbedderTest(unittest.TestCase):
    """Офлайн-эмбеддер детерминирован и не выдаёт себя за e5."""

    def test_deterministic(self) -> None:
        first = HashingEmbedder().encode(["текст примера"], "passage: ")
        second = HashingEmbedder().encode(["текст примера"], "passage: ")
        self.assertTrue(np.allclose(first, second))

    def test_unit_norm(self) -> None:
        vectors = HashingEmbedder().encode(["один", "два"], "passage: ")
        self.assertTrue(np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5))

    def test_similar_texts_are_closer(self) -> None:
        vectors = HashingEmbedder().encode(
            ["нейроморфные чипы", "нейроморфный чип", "открытый банкинг"], "passage: "
        )
        self.assertGreater(float(vectors[0] @ vectors[1]), float(vectors[0] @ vectors[2]))

    def test_model_name_differs_from_e5(self) -> None:
        self.assertNotIn("e5", HashingEmbedder().model_name)


if __name__ == "__main__":
    unittest.main()
