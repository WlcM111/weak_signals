"""Разметка внешними моделями (решение владельца 04.10.2026): синонимы, листы карточек и спорных пар."""

from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from ml.benchmark import cli, expert_match, labeling
from ml.benchmark.snapshot import load_snapshot

from tests.test_benchmark_tools import _snapshot

ROOT = Path(__file__).resolve().parents[2]
EXPERTS = [{"row_id": "pos-1", "label": 1, "domain_tag": "fintech", "title": "Агентские платежи (agent payments)"},
           {"row_id": "pos-2", "label": 1, "domain_tag": "fintech", "title": "Токенизация депозитов"}]


def _human_eval():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("human_eval", ROOT / "tools/quality/human_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class SynonymsTest(unittest.TestCase):
    def test_union_keeps_order_drops_duplicates_short_and_title(self) -> None:
        a = {"pos-1": ["Agent payments protocol", "ИИ"], "pos-2": ["tokenized deposits"]}
        b = {"pos-1": ["agent  payments protocol!", "machine-to-machine payments", "Агентские платежи (agent payments)"],
             "pos-2": []}
        merged, dropped = labeling.merge_synonyms([a, b], EXPERTS)
        self.assertEqual(merged, {"pos-1": ["Agent payments protocol", "machine-to-machine payments"],
                                  "pos-2": ["tokenized deposits"]})
        self.assertEqual(dropped, {"повтор": 1, "короче порога": 1, "равен названию": 1})

    def test_mismatched_rows_are_an_error(self) -> None:
        with self.assertRaises(ValueError):
            labeling.merge_synonyms([{"pos-1": []}], EXPERTS)

    def test_yaml_is_read_by_the_protocol(self) -> None:
        merged = {"pos-1": ["agent payments \"x\"", "оплата: агенты"], "pos-2": []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.yaml"
            path.write_text(labeling.synonyms_yaml(merged, ["тест"]), encoding="utf-8")
            loaded, digest = expert_match.load_synonyms(str(path))
        self.assertEqual(loaded, merged)
        self.assertEqual(len(digest), 64)

    def test_cli_merge_from_model_answers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "d.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in EXPERTS), encoding="utf-8")
            for name, terms in (("a", ["agent payments protocol"]), ("b", ["tokenized bank deposits"])):
                (base / f"{name}.jsonl").write_text(
                    "".join(json.dumps({"row_id": r["row_id"], "synonyms": terms}) + "\n" for r in EXPERTS), encoding="utf-8")
            code = cli.main(["synonyms-merge", "--dataset", str(base / "d.jsonl"), "--answers", str(base / "a.jsonl"),
                             str(base / "b.jsonl"), "--names", "A", "B", "--out", str(base / "s.yaml")])
            loaded, _ = expert_match.load_synonyms(str(base / "s.yaml"))
        self.assertEqual(code, 0)
        self.assertEqual(loaded["pos-1"], ["agent payments protocol", "tokenized bank deposits"])


class CardsTest(unittest.TestCase):
    def _cards(self, folder: Path) -> Path:
        path = folder / "cards.csv"
        labeling.write_csv(path, labeling.CARD_FIELDS[:-2], [
            {"card_id": f"stand-run-1#0#{rank}", "topic": "финтех", "title": f"Карточка {rank}", "description": "",
             "advantage": "", "case_example": "", "sources": ""} for rank in (1, 2, 3)])
        return path

    def test_sheets_feed_human_eval_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cards = self._cards(base)
            for name, codes in (("a", ["R", "R", "N-GEN"]), ("b", ["R", "N-HYP", "N-GEN"])):
                labeling.write_csv(base / f"{name}.csv", ["card_id", "label", "comment"],
                                   [{"card_id": f"stand-run-1#0#{n}", "label": c, "comment": "x"} for n, c in enumerate(codes, 1)])
            labeling.cards_sheets(cards, [base / "a.csv", base / "b.csv"], base / "out")
            report = _human_eval().score(base / "out", (3,))
        self.assertEqual(report["precision_at_3"]["mean_over_topics"], 0.5)  # согласованы #1 (R) и #3 (не R)

    def test_bad_code_or_missing_card_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cards = self._cards(base)
            labeling.write_csv(base / "bad.csv", ["card_id", "label", "comment"],
                               [{"card_id": f"stand-run-1#0#{n}", "label": "YES", "comment": ""} for n in (1, 2, 3)])
            labeling.write_csv(base / "short.csv", ["card_id", "label", "comment"],
                               [{"card_id": "stand-run-1#0#1", "label": "R", "comment": ""}])
            for name in ("bad.csv", "short.csv"):
                with self.assertRaises(ValueError):
                    labeling.cards_sheets(cards, [base / name], base / "out")


class PairsTest(unittest.TestCase):
    def test_export_and_import_feed_match_scoring(self) -> None:
        rows = [{"row_id": "pos-3", "label": 1, "domain_tag": "fintech", "title": "Токенизация депозитов", "source_urls": []}]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            snapshot = load_snapshot(_snapshot(base / "s" if (base / "s").mkdir() is None else base))
            result = expert_match.match([snapshot], rows, {"pos-3": ["обзор рынка"]})
            out = base / "match"
            out.mkdir()
            (out / "matches.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            pairs = expert_match.review_pairs(result)
            expert_match.write_sheets(pairs, out)
            count = labeling.pairs_export(out, [snapshot], base / "pairs.csv")
            exported = labeling.read_csv(base / "pairs.csv")
            bodies = {row["document_id"]: " ".join(str(row.get("body_text") or "").split())[:600]
                      for row in snapshot.jsonl("documents.jsonl")}
            ids = {p["pair_id"]: p["object_id"] for p in pairs}
            for name, decision in (("a", "Y"), ("b", "y")):
                labeling.write_csv(base / f"{name}.csv", ["pair_id", "decision", "comment"],
                                   [{"pair_id": p["pair_id"], "decision": decision, "comment": "та же технология"} for p in pairs])
            labeling.pairs_import(out, [base / "a.csv", base / "b.csv"])
            report = expert_match.score(out)
            labeling.write_csv(base / "bad.csv", ["pair_id", "decision", "comment"],
                               [{"pair_id": p["pair_id"], "decision": "MAYBE", "comment": ""} for p in pairs])
            with self.assertRaises(ValueError):
                labeling.pairs_import(out, [base / "bad.csv", base / "a.csv"])
        self.assertEqual(count, len(pairs))
        self.assertTrue(pairs)
        self.assertTrue(all(row["object_text"] == bodies[ids[row["pair_id"]]] for row in exported if row["object_kind"] == "document"))
        self.assertTrue(all(row["object_title"] for row in exported))
        self.assertEqual(report["confirmed_pairs"], len(pairs))
        self.assertEqual(report["unresolved_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
