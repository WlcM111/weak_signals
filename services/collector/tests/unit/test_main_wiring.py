"""Сборка адаптеров при старте collector: параметры вызова совпадают с объявлением (регрессия v4)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

MAIN = Path(__file__).resolve().parents[2] / "src" / "collector" / "main.py"


class MainWiringTest(unittest.TestCase):
    def test_every_call_matches_function_parameters(self) -> None:
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        params = {node.name: len(node.args.args) for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in params:
                self.assertLessEqual(len(node.args), params[node.func.id],
                                     f"{node.func.id}: передано {len(node.args)} аргументов, объявлено {params[node.func.id]}")

    def test_build_adapters_accepts_arxiv_client(self) -> None:
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        build = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_adapters")
        self.assertIn("arxiv_http_client", [arg.arg for arg in build.args.args])


if __name__ == "__main__":
    unittest.main()
