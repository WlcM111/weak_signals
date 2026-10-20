"""Этап 5.4: инструмент замера финальной проверки зрелости согласован с orchestrator и считает потери верно."""

from __future__ import annotations

import csv
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from orchestrator.application.stages.narrate_rubric import FINAL_DROP, MARKET_EVIDENCE

try:  # пакет insight есть в общем PYTHONPATH make test; в образе orchestrator его нет
    from insight.eval_rubric import MARKET_EVIDENCE as EVAL_MARKET
except ImportError:  # pragma: no cover
    EVAL_MARKET = None

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("final_check_compare",
                                               ROOT / "tools" / "diagnostics" / "final_check_compare.py")
fcc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fcc)

FIELDS = ["id", "label_kind", "pred_code", "reason_ru", "title", "topic"]


def rows(*items: tuple[str, str, str]) -> list[dict]:
    return [{"id": cid, "label_kind": kind, "pred_code": code, "reason_ru": "", "title": cid, "topic": "t"}
            for cid, kind, code in items]


class FinalCheckCompareTest(unittest.TestCase):
    def test_drop_codes_match_orchestrator(self) -> None:
        self.assertEqual(fcc.FINAL_DROP, FINAL_DROP)

    @unittest.skipIf(EVAL_MARKET is None, "нет пакета insight")
    def test_market_split_matches_eval(self) -> None:
        self.assertEqual(EVAL_MARKET, MARKET_EVIDENCE)  # рыночные источники — одно деление в выдаче и в замере

    def test_run_name_and_tally(self) -> None:
        self.assertEqual(fcc.run_of("pf_20261018-3-12"), "pf_20261018")
        count = fcc.tally(rows(("a-0-1", "зрелое", "N-MAT"), ("a-0-2", "зрелое", "R"), ("a-0-3", "ранний", "N-MAT"),
                               ("a-0-4", "слабый сигнал", ""), ("a-0-5", "шум", "N-GEN"), ("a-0-6", "ранний", "U")))
        self.assertEqual((count["mature"], count["mature_removed"], count["weak"], count["weak_removed"]), (2, 1, 3, 1))
        self.assertEqual((count["other_removed"], count["unjudged"]), (1, 1))  # без вердикта — оставлена

    def test_rule_counts_only_check_runs(self) -> None:
        per_run = {"dev": fcc.tally(rows(("dev-0-1", "ранний", "N-MAT"))),
                   "chk": fcc.tally(rows(*[(f"chk-0-{i}", "ранний", "R") for i in range(10)],
                                         ("chk-0-10", "зрелое", "N-MAT")))}
        decision, total = fcc.verdict(per_run, {"dev"})
        self.assertTrue(decision.startswith("ВНЕДРЯТЬ (слабые в допуске"))
        self.assertEqual((total["weak"], total["mature_removed"]), (10, 1))
        per_run["chk"] = fcc.tally(rows(*[(f"chk-0-{i}", "ранний", "N-MAT" if i < 2 else "R") for i in range(10)],
                                        ("chk-0-10", "зрелое", "N-MAT")))
        self.assertIn("ПОТЕРИ СВЕРХ ДОПУСКА", fcc.verdict(per_run, {"dev"})[0])  # потеряно 20 % слабых

    def test_threshold_matches_orchestrator_gate(self) -> None:
        row = {"label_kind": "ранний", "pred_code": "N-MAT", "confidence": "0.6"}
        self.assertTrue(fcc.is_removed(row))
        self.assertFalse(fcc.is_removed(row, 0.7))  # неуверенная зрелость карточку не убирает
        self.assertTrue(fcc.is_removed({**row, "pred_code": "N-GEN"}, 0.7))  # порог — только для N-MAT
        market = {**row, "market_sources": "1"}
        self.assertFalse(fcc.is_removed(market))  # рыночная карточка: N-MAT её не убирает
        self.assertTrue(fcc.is_removed(market, research_only=False))
        per_run = {"dev": rows(("dev-0-1", "ранний", "N-MAT"), ("dev-0-2", "зрелое", "N-MAT"))}
        per_run["dev"][0]["confidence"], per_run["dev"][1]["confidence"] = "0.5", "0.8"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(fcc.sweep(per_run, {"dev"}), 0.55)

    def test_cli_writes_lost_list(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path, lost = Path(folder) / "rubric_b_x_predictions.csv", Path(folder) / "lost.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows(("r-1-1", "ранний", "N-MAT"), ("r-1-2", "зрелое", "N-MAT"),
                                      ("r-1-3", "шум", "R")))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(fcc.main([str(path), "--lost", str(lost)]), 0)
            self.assertIn("потерян слабый: r-1-1", out.getvalue())
            with lost.open(encoding="utf-8") as handle:
                listed = list(csv.DictReader(handle))
            self.assertEqual([row["id"] for row in listed], ["r-1-1", "r-1-2"])


if __name__ == "__main__":
    unittest.main()
