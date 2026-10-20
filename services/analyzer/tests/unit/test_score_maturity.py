"""Этап 5.5: локальная модель зрелости — нет файла модели → пустой ответ; есть → вероятности и порог из файла;
новый файл подхватывается без перезапуска."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from analyzer.application.use_cases.score_maturity import ScoreMaturity


class FakeEncoder:
    def __init__(self) -> None:
        self.prefixes: list[str] = []

    def encode(self, texts, prefix):  # noqa: ANN001, ANN201
        self.prefixes.append(prefix)
        return [[1.0, 0.0] if "зрел" in text else [0.0, 1.0] for text in texts]


def write_model(path: Path, version: str, coef: list[float]) -> None:
    path.write_text(json.dumps({"model": "fake", "prefix": "", "coef": coef, "intercept": 0.0, "threshold": 0.7,
                                "version": version}), encoding="utf-8")


class ScoreMaturityTest(unittest.TestCase):
    def test_without_model_file_nothing_is_scored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scorer = ScoreMaturity(Path(tmp) / "none.json", Path(tmp), embedder_factory=lambda name: FakeEncoder())
            self.assertEqual(scorer.execute(["текст"]), ([], 0.0, ""))

    def test_scores_threshold_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            write_model(path, "v1", [4.0, -4.0])
            encoder = FakeEncoder()
            scorer = ScoreMaturity(path, Path(tmp), embedder_factory=lambda name: encoder)
            scores, threshold, version = scorer.execute(["зрелая область", "новый механизм"])
            self.assertEqual((threshold, version), (0.7, "v1"))
            self.assertGreater(scores[0], 0.98)
            self.assertLess(scores[1], 0.02)
            time.sleep(0.01)
            write_model(path, "v2", [-4.0, 4.0])
            path.touch()
            scores, _, version = scorer.execute(["зрелая область"])
            self.assertEqual(version, "v2")
            self.assertLess(scores[0], 0.02)


if __name__ == "__main__":
    unittest.main()
