"""Этап 4.6 (аудит P01, P02, P27): правила быстрого замера и общая идентичность публикаций."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import date
from pathlib import Path

from ws_common.identity import canonical_url, identity_key, identity_keys

ROOT = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("strategy_check", ROOT / "tools" / "benchmark" / "strategy_check.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)  # type: ignore[union-attr]

ROW_86 = {"row_id": "pos-0086", "title": "Кремний, спроектированный под агентный ИИ (длинный контекст, KV-cache, low-latency decode)",
          "source_urls": ["https://arxiv.org/html/2604.24447v1"], "domain_tag": "ai_infrastructure"}
SYN_86 = ["Agentic AI inference accelerators", "agentic AI inference chips", "purpose-built silicon for agentic AI"]


class IdentityTest(unittest.TestCase):
    def test_arxiv_forms_are_one_paper(self) -> None:
        forms = ["https://arxiv.org/html/2604.24447v1", "https://arxiv.org/abs/2604.24447",
                 "https://arxiv.org/pdf/2604.24447v2.pdf", "http://export.arxiv.org/abs/2604.24447v3"]
        self.assertEqual({identity_key(u) for u in forms}, {"arxiv:2604.24447"})
        self.assertEqual(identity_key("https://ar5iv.labs.arxiv.org/html/2412.02410"), "arxiv:2412.02410")

    def test_query_ids_are_kept_and_tracking_dropped(self) -> None:
        self.assertNotEqual(canonical_url("https://e.com/article?id=1"), canonical_url("https://e.com/article?id=2"))
        self.assertEqual(canonical_url("https://www.e.com/a/?utm_source=x&fbclid=y"), "e.com/a")
        self.assertIn("doi:10.1038/s41598-025-28904-8", identity_keys("https://doi.org/10.1038/S41598-025-28904-8"))


class MatcherTest(unittest.TestCase):
    def test_audit_counterexample_and_positive(self) -> None:
        rules = sc.expert_rules(ROW_86, SYN_86)
        self.assertIsNone(sc.match_document("A Python script fixes KV-cache allocation.", "No new hardware is involved.", rules))
        self.assertEqual(sc.match_document("New inference chip keeps the KV-cache on die", "", rules), ("alias+context", "kv cache"))
        self.assertEqual(sc.match_document("Startup ships agentic AI inference chips", "", rules)[0], "synonym")

    def test_evaluate_counts_url_identity_and_cutoff(self) -> None:
        topic = {"topic": "инфраструктура для искусственного интеллекта и дата-центры", "documents": [
            {"url": "https://arxiv.org/abs/2604.24447", "title": "Paper", "text": "", "source": "arxiv", "published_at": "2026-04-28"},
            {"url": "https://e.com/x", "title": "Python fixes KV-cache allocation", "text": "", "source": "rss", "published_at": None}]}
        result = sc.evaluate([topic], [ROW_86], {"pos-0086": SYN_86}, None, date(2026, 10, 11))
        self.assertEqual((result["totals"]["found"], result["totals"]["organizer_url"]), (1, 1))
        self.assertEqual(result["rows"][0][6], "organizer_url")  # ложное совпадение по KV-cache не засчитано
        late = sc.evaluate([topic], [ROW_86], {"pos-0086": SYN_86}, date(2026, 4, 1), date(2026, 10, 11))
        self.assertEqual(late["totals"]["found"], 0)  # статья после даты среза исключена

    def test_temporal_eligibility(self) -> None:
        self.assertFalse(sc.temporal_eligible(["https://geekwire.com/2022/05/12/tignis/"], date(2024, 10, 11)))
        self.assertTrue(sc.temporal_eligible(["https://techcrunch.com/2026/03/01/x/"], date(2024, 10, 11)))
        self.assertIsNone(sc.temporal_eligible(["https://pulse2.com/x/"], date(2024, 10, 11)))


if __name__ == "__main__":
    unittest.main()
