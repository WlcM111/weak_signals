"""Первый этап: честная оценка классификатора (аудит A1, A2, D2)."""

import unittest

from ml import signal_classifier_train as t


def row(label: int, text: str, origin: str = "o", domain: str = "d") -> dict:
    return {"label": label, "title": text, "description": text, "origin": origin, "annotators": [origin],
            "domain_tag": domain}


class EvaluationIntegrityTest(unittest.TestCase):
    def test_wilson_matches_audit_and_handles_zero(self) -> None:
        self.assertEqual(t.wilson(38, 45), [0.7122, 0.9225])  # аудит: 38/45 → [0,712; 0,923]
        self.assertIsNone(t.wilson(0, 0))

    def test_scores_confusion_ci_and_undefined_recall(self) -> None:
        s = t.scores([1, 1, 0, 0], [1, 0, 0, 1])
        self.assertEqual(s["confusion"], {"tp": 1, "fp": 1, "fn": 1, "tn": 1})
        self.assertEqual(s["majority_accuracy"], 0.5)
        none_pos = t.scores([0, 0], [0, 1])
        self.assertIsNone(none_pos["recall"])  # нет позитивов — recall не определён, а не 0
        self.assertIsNone(none_pos["ci95"]["recall"])

    def test_confound_detected_only_when_class_equals_origin(self) -> None:
        confounded = [row(1, "a", "gpb"), row(1, "b", "gpb"), row(0, "c", "team"), row(0, "d", "team")]
        mixed = confounded + [row(0, "e", "gpb")]
        self.assertTrue(t.origin_confound(confounded)["origin"]["class_equals_field"])
        self.assertFalse(t.origin_confound(mixed)["origin"]["class_equals_field"])
        self.assertFalse(t.origin_confound([row(1, "a"), row(0, "b")])["origin"]["class_equals_field"])  # один источник

    def test_length_auc(self) -> None:
        rows = [row(1, "длинный текст " * 10), row(1, "длинный текст " * 8), row(0, "кор"), row(0, "крт")]
        self.assertEqual(t.length_auc(rows), 1.0)
        self.assertIsNone(t.length_auc([row(1, "a"), row(1, "b")]))

    def test_leave_one_domain_out_handles_domain_without_positives(self) -> None:
        rows = [row(1, f"первые пилоты прототип {i}", domain="a") for i in range(4)] + \
               [row(0, f"массовое внедрение стандарт {i}", domain="a") for i in range(4)] + \
               [row(1, f"первые испытания прототип {i}", domain="b") for i in range(4)] + \
               [row(0, f"массовое внедрение лидеры {i}", domain="c") for i in range(4)]
        result = t.cross_by_group(rows, [])
        self.assertEqual(set(result["per_group"]), {"a", "b", "c"})
        self.assertIsNone(result["per_group"]["c"]["recall"])  # в домене c нет позитивов
        self.assertEqual(result["n"], len(rows))
        self.assertIn("error", t.cross_by_group([row(1, "x"), row(0, "y")], []))


if __name__ == "__main__":
    unittest.main()
