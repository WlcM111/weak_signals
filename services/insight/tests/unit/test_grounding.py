"""Проверка обоснованности нарратива: числа, ссылки, документы, признаки, язык (§6 HANDOFF)."""

from __future__ import annotations

import unittest

from insight.domain import grounding
from insight.domain.entities import Narrative
from insight.domain.values import TrustLevel

from ..fakes import make_candidate, make_evidence, new_id

EVIDENCE = [make_evidence(1), make_evidence(2, language="ru")]
FEATURES = make_candidate(features=2).top_features


def narrative(**overrides: str) -> Narrative:
    """Обоснованный нарратив с точечными изменениями."""
    payload = {
        "title_ru": "Нейроморфные чипы",
        "description_ru": (
            "Источники описывают прототип, испытанный в лабораториях [doc:1]. "
            "Пилот охватил 37 устройств [doc:1]."
        ),
        "advantage_ru": "Снижение энергопотребления на 45 процентов по данным источника [doc:1].",
        "case_example_ru": "Команда закрыла посевной раунд на 12 миллионов долларов [doc:1].",
        "explanation_ru": (
            "Модель отнесла наблюдение к слабому сигналу: лексика ранней стадии равна 0.82, "
            "а доля доверенных источников составляет 0.60."
        ),
        "case_document_id": EVIDENCE[0].document_id,
    }
    payload.update(overrides)
    return Narrative(**payload)  # type: ignore[arg-type]


class NumberExtractionTest(unittest.TestCase):
    """Какие числа обязаны подтверждаться доказательствами."""

    def test_years_are_ignored(self) -> None:
        self.assertEqual(grounding.extract_numbers("В 2026 году и в 2019 году"), [])

    def test_small_numbers_are_ignored(self) -> None:
        self.assertEqual(grounding.extract_numbers("две компании, 1 проект, 2 отчёта"), [])

    def test_significant_numbers_extracted(self) -> None:
        self.assertEqual(grounding.extract_numbers("рост 45 процентов и 12 миллионов"), [45.0, 12.0])

    def test_decimal_with_comma(self) -> None:
        self.assertEqual(grounding.extract_numbers("коэффициент 3,5"), [3.5])

    def test_year_outside_range_is_significant(self) -> None:
        self.assertEqual(grounding.extract_numbers("в 1998 году"), [1998.0])


class NumberSupportTest(unittest.TestCase):
    """Числа нарратива сверяются с текстами доказательств."""

    def test_supported_numbers_pass(self) -> None:
        self.assertEqual(grounding.unsupported_numbers("рост 45 процентов", EVIDENCE), [])

    def test_invented_number_detected(self) -> None:
        self.assertEqual(grounding.unsupported_numbers("рост 99 процентов", EVIDENCE), [99.0])


class DocumentRefTest(unittest.TestCase):
    """Ссылки `[doc:N]` и удаление их из выдачи."""

    def test_known_refs_pass(self) -> None:
        self.assertEqual(grounding.unknown_document_refs("текст [doc:1] и [doc:2]", 2), [])

    def test_unknown_ref_detected(self) -> None:
        self.assertEqual(grounding.unknown_document_refs("текст [doc:5]", 2), [5])

    def test_strip_doc_refs(self) -> None:
        self.assertEqual(grounding.strip_doc_refs("Прототип [doc:1] готов."), "Прототип готов.")


class CyrillicShareTest(unittest.TestCase):
    """Доля кириллицы определяет, на каком языке написан текст."""

    def test_russian_text(self) -> None:
        self.assertGreater(grounding.cyrillic_share("Полностью русский текст"), 0.9)

    def test_english_text(self) -> None:
        self.assertLess(grounding.cyrillic_share("Completely English text"), 0.1)

    def test_text_without_letters(self) -> None:
        self.assertEqual(grounding.cyrillic_share("42 -- 17"), 1.0)


class FeatureMentionTest(unittest.TestCase):
    """Признаки узнаются по основам слов метки."""

    def test_exact_mention(self) -> None:
        text = "Лексика ранней стадии высокая, доля доверенных источников умеренная."
        self.assertEqual(grounding.count_mentioned_features(text, FEATURES), 2)

    def test_inflected_mention(self) -> None:
        text = "Лексики ранней стадии достаточно, доли доверенных источников хватает."
        self.assertEqual(grounding.count_mentioned_features(text, FEATURES), 2)

    def test_missing_mention(self) -> None:
        self.assertEqual(grounding.count_mentioned_features("Ничего о признаках", FEATURES), 0)


class GroundingCheckTest(unittest.TestCase):
    """Таблица случаев: что считается жёстким нарушением, а что мягким."""

    def test_valid_narrative_passes(self) -> None:
        check = grounding.check(narrative(), EVIDENCE, FEATURES)
        self.assertTrue(check.passed, check.summary)
        self.assertEqual(check.features_mentioned, 2)

    def test_invented_number_is_hard_failure(self) -> None:
        check = grounding.check(
            narrative(advantage_ru="Рынок вырастет на 300 процентов."), EVIDENCE, FEATURES
        )
        self.assertFalse(check.passed)
        self.assertTrue(check.has_hard_failure)
        self.assertEqual(check.unsupported_numbers, 1)

    def test_unknown_doc_ref_is_hard_failure(self) -> None:
        check = grounding.check(narrative(description_ru="Прототип [doc:9]."), EVIDENCE, FEATURES)
        self.assertTrue(check.has_hard_failure)
        self.assertEqual(check.unknown_document_refs, 1)

    def test_foreign_case_document_is_hard_failure(self) -> None:
        check = grounding.check(narrative(case_document_id=new_id(777)), EVIDENCE, FEATURES)
        self.assertTrue(check.has_hard_failure)

    def test_missing_features_is_soft_failure(self) -> None:
        check = grounding.check(
            narrative(explanation_ru="Технология выглядит перспективной по мнению аналитиков."),
            EVIDENCE,
            FEATURES,
        )
        self.assertFalse(check.passed)
        self.assertFalse(check.has_hard_failure)
        self.assertTrue(check.soft_failures)

    def test_latin_text_is_soft_failure(self) -> None:
        check = grounding.check(
            narrative(description_ru="The prototype was tested in laboratories."), EVIDENCE, FEATURES
        )
        self.assertFalse(check.passed)
        self.assertFalse(check.has_hard_failure)

    def test_missing_case_document_is_soft_failure(self) -> None:
        check = grounding.check(narrative(case_document_id=""), EVIDENCE, FEATURES)
        self.assertFalse(check.passed)
        self.assertFalse(check.has_hard_failure)

    def test_summary_lists_violations(self) -> None:
        check = grounding.check(narrative(advantage_ru="Рост на 300 процентов."), EVIDENCE, FEATURES)
        self.assertIn("300", check.summary)

    def test_low_trust_evidence_still_grounds_numbers(self) -> None:
        evidence = [make_evidence(3, trust=TrustLevel.LOW)]
        check = grounding.check(
            narrative(
                description_ru="Пилот охватил 37 устройств [doc:1].",
                advantage_ru="Снижение энергопотребления на 45 процентов [doc:1].",
                case_example_ru="Посевной раунд на 12 миллионов долларов [doc:1].",
                case_document_id=evidence[0].document_id,
            ),
            evidence,
            FEATURES,
        )
        self.assertTrue(check.passed, check.summary)


if __name__ == "__main__":
    unittest.main()
