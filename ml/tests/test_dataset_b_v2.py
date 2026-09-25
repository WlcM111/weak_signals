"""Датасет B v2: выгрузка для экспертов, согласие, арбитраж, итоговые метки и сборка (без тяжёлых зависимостей)."""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from ml.dataset_b import v2
from ml.dataset_b.grouping import group_observations
from ml.dataset_b.runs_parser import parse_analytics

OLD_TOPIC, OTHER_TOPIC = "тема альфа", "тема бета"
NEW_HOLDOUT = ("новая тема один", "новая тема два", "новая тема три")


def _analytics(blocks: list[tuple[str, list[tuple[str, str, list[str]]]]]) -> str:
    lines = ["АНАЛИТИКА ПО ШЕСТИ ТЕМАМ ДАТАСЕТА — прогон тест UTC, top_n = 15", ""]
    for index, (topic, cards) in enumerate(blocks, 1):
        lines += [f"ТЕМА {index}/{len(blocks)}: Домен — запрос «{topic}»", f"job_id: job-{index}   top_n: 15",
                  f"ВЫДАЧА: {len(cards)} технологий"]
        for rank, (title, auto, urls) in enumerate(cards, 1):
            lines += [f"  #{rank} [GENERATED | High | скоринг {90 - rank:.1f}%] {title}",
                      f"     автоназвание кластера: {auto}", f"     источники ({len(urls)}):"]
            for url in urls:
                lines += [f"       - [openalex | SCIENTIFIC_PUBLICATION | HIGH | en | 2025-01-01 | "
                          f"GENERATIVE_SUMMARY] Paper {url}", f"         {url}"]
        lines.append("ИСКЛЮЧЁННЫЕ КАНДИДАТЫ: 0")
    return "\n".join(lines) + "\n"


def _own_topics(topics: tuple[str, ...]) -> str:
    blocks = []
    for index, topic in enumerate(topics, 1):
        cards = [{"item_id": f"i{index}{rank}", "rank": rank, "title_auto": f"new tech {index}{rank}",
                  "title_ru": f"Новая технология {index}{rank}", "narrative_status": "GENERATED", "score": 0.8,
                  "confidence_band": "High", "features": [],
                  "sources": [{"title": f"Paper {index}{rank}", "url": f"https://new.example/{index}/{rank}",
                               "source_key": "openalex", "source_type": "SCIENTIFIC_PUBLICATION",
                               "trust_level": "HIGH", "language_code": "en", "published_at": "2025-02-01"}]}
                 for rank in (1, 2)]
        blocks.append({"topic": topic, "job": {"job_id": f"j{index}", "finished_at": "2026-09-26T10:00:00+00:00"},
                       "cards": cards})
    return json.dumps(blocks, ensure_ascii=False)


def _sheet(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write(path: Path, rows: list[dict[str, str]], delimiter: str = ",", bom: bool = True,
           title_row: bool = False) -> None:
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        if title_row:
            handle.write("Таблица 1\n")
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


class Fixture:
    """Проект с прогоном v1, новым прогоном (смена id групп и конфликт silver) и прогоном новых тем."""

    def __init__(self, tmp: Path) -> None:
        self.root, self.b = tmp / "project", tmp / "b"
        self.root.mkdir()
        (self.b / "v2").mkdir(parents=True)
        v1_run = _analytics([
            (OLD_TOPIC, [("Зета", "zeta cluster", ["https://a.example/1"]),
                         ("Эта", "eta cluster", ["https://a.example/2"]),
                         ("Тета", "theta cluster", ["https://a.example/3"])]),
            (OTHER_TOPIC, [("Йота", "iota cluster", ["https://b.example/1"])]),
        ])
        (self.root / "analytics-20260901-100000.txt").write_text(v1_run, encoding="utf-8")
        items = [asdict(i) for i in parse_analytics(self.root / "analytics-20260901-100000.txt")[0]]
        gmap = group_observations(items)
        codes = {"zeta cluster": "R", "eta cluster": "N-OVR", "theta cluster": "R", "iota cluster": "N-GEN"}
        self.v1_gid = {item["title_auto"]: gmap[item["obs_id"]] for item in items}
        dup = [{"group_id": gmap[i["obs_id"]], "obs_id": i["obs_id"], "run_id": i["run_id"], "rank": i["rank"],
                "representative": True} for i in items]
        labels = [{"group_id": self.v1_gid[t], "code": c, "confidence": 0.7, "rationale": f"silver {t}"}
                  for t, c in codes.items()]
        self._jsonl(self.b / "duplicates_manifest_v1.jsonl", dup)
        self._jsonl(self.b / "labels_silver_v1.jsonl", labels)
        self._jsonl(self.b / "dataset_b_v1.jsonl", [{"topic": OLD_TOPIC}, {"topic": OTHER_TOPIC}])
        self._jsonl(self.b / "dataset_b_v1_uncertain.jsonl", [])
        self._jsonl(self.b / "web_observations_v1.jsonl", [{
            "web_id": "bweb-sample-pilot", "topic": OTHER_TOPIC, "title": "sample pilot", "code": "N-HYP",
            "confidence": 0.6, "rationale": "web silver", "counterevidence": "",
            "evidence": [{"title": "Pilot news", "url": "https://w.example/1", "source_type": "INDUSTRY_MEDIA",
                          "trust_level": "MEDIUM", "published_at": "2026-05-01", "language_code": "en"}]}])
        new_run = _analytics([
            (OLD_TOPIC, [("Альфа", "alpha cluster", ["https://a.example/1"]),
                         ("Мост", "bridge cluster", ["https://a.example/2", "https://a.example/3"]),
                         ("Новая", "brand new cluster", ["https://a.example/9"])]),
        ])
        (self.root / "analytics-20260902-100000.txt").write_text(new_run, encoding="utf-8")
        (self.root / "own-topics-20260903-100000.json").write_text(_own_topics(NEW_HOLDOUT), encoding="utf-8")
        (self.b / "v2" / "holdout_topics.txt").write_text("\n".join(NEW_HOLDOUT) + "\n", encoding="utf-8")

    @staticmethod
    def _jsonl(path: Path, rows: list[dict]) -> None:
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def v1_hashes(self) -> dict[str, str]:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(self.b.glob("*.jsonl"))}


class HelpersTest(unittest.TestCase):
    def test_kappa_known_values(self) -> None:
        self.assertEqual(v2.cohen_kappa(["R", "N-OFF", "R"], ["R", "N-OFF", "R"]), 1.0)
        # Классический пример: 20 объектов, наблюдаемое согласие 0,7, ожидаемое 0,5 → каппа 0,4.
        first = ["R"] * 5 + ["R"] * 5 + ["N"] * 5 + ["N"] * 5
        second = ["R"] * 5 + ["N"] * 5 + ["R"] * 1 + ["N"] * 9
        self.assertAlmostEqual(v2.cohen_kappa(first, second), 0.4, places=6)
        self.assertIsNone(v2.cohen_kappa(["R", "R"], ["R", "R"][:1]))
        self.assertIsNone(v2.cohen_kappa([], []))

    def test_codes_and_russian_layout(self) -> None:
        self.assertEqual(v2.normalize_code(" r "), ("R", False))
        self.assertEqual(v2.normalize_code("n_off"), ("N-OFF", False))
        self.assertEqual(v2.normalize_code("к"), ("R", True))
        self.assertEqual(v2.normalize_code("т-щмк"), ("N-OVR", True))
        self.assertEqual(v2.normalize_code("да"), ("?", True))
        self.assertEqual(v2.normalize_code(""), (None, False))

    def test_wilson_interval(self) -> None:
        low, high = v2.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=3)
        self.assertAlmostEqual(high, 0.7634, places=3)
        self.assertEqual(v2.wilson(0, 0), (None, None))

    def test_read_sheet_after_spreadsheet_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.csv"
            _write(path, [{"group_id": "g1", "label": "R", "comment": "да; точно"}], delimiter=";",
                   title_row=True)
            self.assertEqual(v2.read_sheet(path), [{"group_id": "g1", "label": "R", "comment": "да; точно"}])
            path.write_bytes("group_id,label\ng2,N-OFF\n".encode("cp1251"))
            self.assertEqual(v2.read_sheet(path), [{"group_id": "g2", "label": "N-OFF"}])


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))
        self.lab = self.fx.b / "v2" / "labeling"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _export(self) -> dict:
        return v2.export_sheets(self.fx.root, self.fx.b)

    def _manifest(self) -> dict:
        return json.loads((self.lab / "manifest.json").read_text(encoding="utf-8"))

    def test_topics_check_rejects_known_and_short_lists(self) -> None:
        self.assertEqual(v2.topics_check(self.fx.b)["status"], "ok")
        (self.fx.b / "v2" / "holdout_topics.txt").write_text(f"{OLD_TOPIC}\nновая\n", encoding="utf-8")
        problems = v2.topics_check(self.fx.b)["problems"]
        self.assertTrue(any("уже есть в датасете B v1" in p for p in problems))
        self.assertTrue(any("нужно не меньше 3" in p for p in problems))

    def test_export_carries_silver_by_membership_and_blinds_sheets(self) -> None:
        summary = self._export()
        manifest = self._manifest()
        groups = manifest["groups"]
        alpha = next(g for g, r in groups.items() if r["topic"] == OLD_TOPIC and len(r["members"]) == 2
                     and not r["silver_conflict"] and r["silver"] and r["silver"]["code"] == "R")
        self.assertNotIn(alpha, self.fx.v1_gid.values(), "id группы сменился: метка перенесена по составу")
        bridge = [r for r in groups.values() if r["silver_conflict"]]
        self.assertEqual(len(bridge), 1)
        self.assertEqual(bridge[0]["silver_conflict"], ["N-OVR", "R"])
        self.assertTrue(bridge[0]["required"])
        self.assertEqual(summary["holdout_groups_by_topic"], {t: 2 for t in sorted(NEW_HOLDOUT)})
        self.assertEqual((summary["v1_labels"], summary["v1_labels_found"], summary["v1_labels_lost"]), (4, 4, []))
        rows = _sheet(self.lab / "expert1.csv")
        self.assertEqual(list(rows[0]), v2.SHEET_FIELDS)
        self.assertEqual((self.lab / "expert1.csv").read_bytes(), (self.lab / "expert2.csv").read_bytes())
        self.assertTrue(all(r["label"] == "" for r in rows))
        text = (self.lab / "expert1.csv").read_text(encoding="utf-8-sig")
        self.assertNotIn("silver", text)
        self.assertEqual([r["queue"] for r in rows], sorted(r["queue"] for r in rows))
        with self.assertRaises(v2.DatasetV2Error):
            self._export()

    def _label_all(self, disagree_on: set[str]) -> None:
        manifest = self._manifest()
        rows = _sheet(self.lab / "expert1.csv")
        first, second = [], []
        for row in rows:
            record = manifest["groups"][row["group_id"]]
            code = "R" if record["holdout"] or "new" in row["cluster_title"] else (
                record["silver"]["code"] if record["silver"] else "N-GEN")
            first.append({**row, "label": code, "comment": "эксперт 1"})
            other = "N-OFF" if row["group_id"] in disagree_on else code
            second.append({**row, "label": "к" if other == "R" else other, "comment": "эксперт 2"})
        _write(self.lab / "expert1.csv", first)
        _write(self.lab / "expert2.csv", second, delimiter=";", bom=False)

    def test_full_flow_agreement_arbitration_merge_build(self) -> None:
        before = self.fx.v1_hashes()
        self._export()
        manifest = self._manifest()
        target = next(g for g, r in manifest["groups"].items() if r["holdout"])
        self._label_all({target})
        report = v2.agreement(self.fx.b)
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["disagreements"], 1)
        self.assertEqual(report["disagreements_on_R"], 1)
        self.assertTrue(report["layout_fixed"])
        self.assertLess(report["kappa_codes"], 1.0)
        self.assertEqual(v2.merge(self.fx.b)["status"], "problems")
        arbitration = _sheet(self.lab / "arbitration.csv")
        self.assertEqual([r["group_id"] for r in arbitration], [target])
        _write(self.lab / "arbitration.csv", [{**arbitration[0], "label": "R", "comment": "арбитр"}])
        with self.assertRaises(v2.DatasetV2Error):
            v2.agreement(self.fx.b)
        merged = v2.merge(self.fx.b)
        self.assertEqual(merged["status"], "ok", merged)
        self.assertEqual(merged["arbitrated"], 1)
        entry = next(json.loads(x) for x in (self.fx.b / "v2" / "labels_v2.jsonl").read_text(
            encoding="utf-8").splitlines() if json.loads(x)["group_id"] == target)
        self.assertEqual(entry["votes"], {"expert1": "R", "expert2": "N-OFF", "arbiter": "R"})
        self.assertAlmostEqual(entry["confidence"], 0.667, places=3)
        built = v2.build_v2(self.fx.root, self.fx.b)
        self.assertEqual(built["status"], "ok", built["problems"])
        rows = [json.loads(x) for x in (self.fx.b / "v2" / "dataset_b_v2.jsonl").read_text(
            encoding="utf-8").splitlines()]
        holdout = [r for r in rows if r["split"] == "holdout"]
        self.assertEqual({r["topic"] for r in holdout}, set(NEW_HOLDOUT))
        self.assertTrue(all(r["review_status"] == "reviewed" and r["dataset_version"] == "dsb-v2" for r in rows))
        self.assertTrue(all(r["split"].startswith("dev_fold_") for r in rows if r["topic"] == OLD_TOPIC))
        self.assertEqual(before, self.fx.v1_hashes(), "файлы v1 не должны меняться")
        again = v2.build_v2(self.fx.root, self.fx.b)
        self.assertEqual(again["output_sha256"], built["output_sha256"], "сборка детерминирована")

    def test_build_uses_silver_outside_holdout_and_blocks_unlabeled_holdout(self) -> None:
        self._export()
        manifest = self._manifest()
        required = [g for g, r in manifest["groups"].items() if r["required"] and not r["holdout"]]
        rows = [r for r in _sheet(self.lab / "expert1.csv") if r["group_id"] in required]
        labeled = [{**r, "label": "N-NOI"} for r in rows]
        others = [{**r, "label": ""} for r in _sheet(self.lab / "expert1.csv") if r["group_id"] not in required]
        _write(self.lab / "expert1.csv", labeled + others)
        _write(self.lab / "expert2.csv", labeled + others)
        self.assertEqual(v2.agreement(self.fx.b)["disagreements"], 0)
        self.assertEqual(v2.merge(self.fx.b)["status"], "ok")
        report = v2.build_v2(self.fx.root, self.fx.b)
        self.assertEqual(report["status"], "problems")
        self.assertTrue(all("holdout-темы" in p for p in report["problems"] if "без экспертной метки" in p))
        self.assertFalse((self.fx.b / "v2" / "dataset_b_v2.jsonl").exists(), "при проблемах датасет не пишется")
        self.assertEqual(report["label_sources"].get("llm_assisted_silver"), 3)

    def test_build_refuses_changed_inputs(self) -> None:
        self._export()
        run = self.fx.root / "analytics-20260902-100000.txt"
        run.write_text(run.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(v2.DatasetV2Error):
            v2.build_v2(self.fx.root, self.fx.b)

    def test_runs_compare_sheet_and_precision(self) -> None:
        out = Path(self._tmp.name) / "q" / "gate.csv"
        result = v2.export_runs([self.fx.root / "analytics-20260901-100000.txt",
                                 self.fx.root / "analytics-20260902-100000.txt"], out)
        self.assertEqual(result["groups_to_label"], 4)
        rows = _sheet(out)
        _write(out, [{**r, "label": "R" if "zeta" in r["cluster_title"] or "alpha" in r["cluster_title"]
                      else "N-OFF"} for r in rows])
        report = v2.precision(out, out.with_suffix(".manifest.json"))
        both = next(v for k, v in report["by_run"].items() if k.startswith("в обоих"))
        self.assertEqual((both["labeled"], both["R"]), (2, 1))
        self.assertEqual(report["by_run"]["20260902-100000"]["labeled"], 3)


if __name__ == "__main__":
    unittest.main()
