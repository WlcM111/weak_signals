"""Этап 3, B1: эксперимент «классификатор без зависимости от длины» не меняет экспортируемую модель."""

from __future__ import annotations

import unittest

from ml import signal_classifier_train as train


def rows(n: int) -> list[dict]:
    data = []
    for i in range(n):
        positive = i % 2 == 0
        text = ("пилот прототип стартап новый метод " if positive else "обзор рынка зрелая технология ") * (1 + i % 4)
        data.append({"title": text, "description": "", "label": int(positive), "domain_tag": f"d{i % 3}",
                     "origin": "a" if positive else "b", "annotators": ["x"]})
    return data


class B1Test(unittest.TestCase):
    def test_binary_features_ignore_length(self) -> None:
        short = train.binary_features("пилот прототип")
        long = train.binary_features("пилот прототип " * 5)
        self.assertEqual(set(short.values()), {1.0})
        self.assertEqual(set(long.values()), {1.0})  # вес признака одинаков при любой длине текста
        self.assertNotEqual(set(train.base_features("пилот прототип стартап").values()), {1.0})

    def test_experiment_restores_features_and_reports_both_variants(self) -> None:
        original = train.features
        result = train.b1_length_free(rows(30), rows(24))
        self.assertIs(train.features, original)
        self.assertIn("sqrt_current", result)
        self.assertIn("binary_length_free", result)
        self.assertIn("не принимается", result["decision"])
        for variant in ("sqrt_current", "binary_length_free"):
            self.assertIn("accuracy", result[variant]["organizers_cv"])


if __name__ == "__main__":
    unittest.main()
