"""Регрессия подтверждённых дефектов качества карточек (docs/quality/ERROR_ANALYSIS.md, E1–E4).

E1. Ссылки `[doc:N]` засчитывались как «числа, отсутствующие в источниках»: 19 из 23 отказов
    в логе insight от 22.09 имели вид «числа, отсутствующие в источниках: 3, 4, 5».
E2. Значения признаков и оценка, которые промпт требует назвать, не считались подтверждёнными.
E3. Ключ кеша не зависел от запроса, признаков, текста документов и версии шаблона.
E4. Промпт требовал «ровно семь ключей» при шести полях схемы.

Документы здесь намеренно не содержат цифр 3–8 ни в тексте, ни в заголовке: в общих фикстурах
текст «in {index} laboratories» и заголовок «Research paper {index}» случайно подтверждали номер
ссылки и маскировали дефект E1.
"""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import replace

from insight.adapters.outbound.llm.fake_provider import FakeProvider
from insight.application.prompt_builder import PromptBuilder
from insight.application.use_cases.generate_insight import (
    GenerateConfig,
    GenerateInsight,
    GenerateInsightCommand,
    input_hash,
)
from insight.domain import grounding
from insight.domain.entities import FeatureContribution, Narrative
from insight.domain.values import FeatureDirection, InsightStatus

from ..fakes import FakeClock, InMemoryInsightRepository, make_candidate, make_evidence
from .test_chain_and_use_cases import PROMPTS, SCHEMAS, build_chain

CLEAN_TEXT = "Researchers demonstrated a neuromorphic prototype; the pilot reduced energy use by 45 percent."
EVIDENCE = tuple(
    replace(make_evidence(index), title=f"Neuromorphic research paper {letter}", text=CLEAN_TEXT)
    for index, letter in zip(range(1, 9), "ABCDEFGH", strict=True)
)
CANDIDATE = make_candidate(features=2)


def narrative(**overrides: str) -> Narrative:
    """Обоснованный нарратив со ссылками на документы 3–8."""
    payload = {
        "title_ru": "Нейроморфные чипы",
        "description_ru": "Прототип показан в лаборатории [doc:3], пилот снизил энергопотребление [doc:4].",
        "advantage_ru": "Снижение энергопотребления на 45 процентов по данным источников [doc:5] и [doc:8].",
        "case_example_ru": "Пилотное внедрение прототипа описано в источнике [doc:6].",
        "explanation_ru": (
            "Модель отнесла наблюдение к слабому сигналу: лексика ранней стадии равна 0.82, "
            "а доля доверенных источников составляет 0.60 [doc:7]."
        ),
        "case_document_id": EVIDENCE[5].document_id,
    }
    payload.update(overrides)
    return Narrative(**payload)  # type: ignore[arg-type]


class DocumentReferencesTest(unittest.TestCase):
    """E1: номер ссылки на документ — не фактическое число."""

    def test_doc_refs_are_not_counted_as_numbers(self) -> None:
        check = grounding.check(narrative(), EVIDENCE, CANDIDATE.top_features)
        self.assertEqual(check.unsupported_numbers, 0, check.hard_failures)
        self.assertTrue(check.passed, (check.hard_failures, check.soft_failures))

    def test_real_unsupported_number_is_still_flagged(self) -> None:
        check = grounding.check(
            narrative(description_ru="Пилот снизил энергопотребление на 73 процента [doc:3]."),
            EVIDENCE,
            CANDIDATE.top_features,
        )
        self.assertEqual(check.unsupported_numbers, 1)
        self.assertIn("73", check.hard_failures[0])
        self.assertNotIn("3,", check.hard_failures[0])

    def test_reference_outside_evidence_is_still_flagged(self) -> None:
        check = grounding.check(
            narrative(description_ru="Прототип показан в лаборатории [doc:9]."), EVIDENCE, CANDIDATE.top_features
        )
        self.assertEqual(check.unknown_document_refs, 1)
        self.assertFalse(check.passed)


class ModelInputNumbersTest(unittest.TestCase):
    """E2: объяснение может цитировать полученные значения признаков и оценку — и только их."""

    FEATURES = (
        FeatureContribution(
            "org_count", "Число упомянутых организаций", 5.0, -0.74, FeatureDirection.SUPPORTS_MATURE
        ),
        FeatureContribution(
            "lex_emergence_score", "Лексика ранней стадии", 0.82, 0.41, FeatureDirection.SUPPORTS_WEAK_SIGNAL
        ),
    )

    def test_feature_value_allowed_in_explanation(self) -> None:
        text = (
            "Модель учла, что число упомянутых организаций равно 5, "
            "а лексика ранней стадии составляет 0.82."
        )
        check = grounding.check(narrative(explanation_ru=text), EVIDENCE, self.FEATURES, score=0.9976)
        self.assertEqual(check.unsupported_numbers, 0, check.hard_failures)

    def test_feature_value_not_allowed_as_fact_in_description(self) -> None:
        check = grounding.check(
            narrative(description_ru="Технологию развивают 5 организаций [doc:3]."), EVIDENCE, self.FEATURES
        )
        self.assertEqual(check.unsupported_numbers, 1)

    def test_score_percent_allowed_in_explanation_only(self) -> None:
        explanation = (
            "Уверенность модели 99.8 процента: лексика ранней стадии равна 0.82, "
            "число упомянутых организаций равно 5."
        )
        allowed = grounding.check(narrative(explanation_ru=explanation), EVIDENCE, self.FEATURES, score=0.9976)
        self.assertEqual(allowed.unsupported_numbers, 0, allowed.hard_failures)
        in_description = grounding.check(
            narrative(description_ru="Уверенность 99.8 процента [doc:3]."), EVIDENCE, self.FEATURES, score=0.9976
        )
        self.assertEqual(in_description.unsupported_numbers, 1)

    def test_contribution_is_not_given_to_model_and_cannot_be_cited(self) -> None:
        text = "Вклад числа упомянутых организаций составил 0.74, лексика ранней стадии равна 0.82; вклад 7.4."
        check = grounding.check(narrative(explanation_ru=text), EVIDENCE, self.FEATURES, score=0.9976)
        self.assertEqual(check.unsupported_numbers, 1)
        self.assertIn("7.4", check.hard_failures[0])


class InputHashTest(unittest.TestCase):
    """E3: ключ кеша меняется вместе с любым входом, определяющим ответ модели."""

    def digest(  # noqa: ANN001
        self, candidate=CANDIDATE, evidence=EVIDENCE[:2], version: str = "insight_v1@a@b"
    ) -> str:
        return input_hash(candidate, evidence, version)

    def test_same_input_same_key(self) -> None:
        self.assertEqual(self.digest(), self.digest())

    def test_query_changes_key(self) -> None:
        self.assertNotEqual(self.digest(), self.digest(candidate=replace(CANDIDATE, query_text="другой запрос")))

    def test_feature_value_changes_key(self) -> None:
        changed = replace(
            CANDIDATE, top_features=(replace(CANDIDATE.top_features[0], value=0.11),) + CANDIDATE.top_features[1:]
        )
        self.assertNotEqual(self.digest(), self.digest(candidate=changed))

    def test_document_text_changes_key(self) -> None:
        edited = (replace(EVIDENCE[0], text="Изменённый текст источника."), EVIDENCE[1])
        self.assertNotEqual(self.digest(), self.digest(evidence=edited))

    def test_template_version_changes_key(self) -> None:
        self.assertNotEqual(self.digest(), self.digest(version="insight_v1@c@b"))

    def test_generator_uses_template_and_schema_hashes(self) -> None:
        prompts = PromptBuilder(PROMPTS, SCHEMAS)
        use_case = GenerateInsight(
            build_chain([FakeProvider()]), prompts, InMemoryInsightRepository(), FakeClock(), GenerateConfig()
        )
        identity = use_case._prompt_identity  # noqa: SLF001 - проверка состава ключа
        self.assertIn(prompts.template_sha256("insight_v1.j2")[:16], identity)
        self.assertIn(prompts.schema_sha256("insight_llm_output.schema.json")[:16], identity)


class PromptSchemaConsistencyTest(unittest.TestCase):
    """E4: число ключей, названное в промпте, совпадает со схемой."""

    def test_prompt_lists_exactly_the_schema_keys(self) -> None:
        template = (PROMPTS / "insight_v1.j2").read_text(encoding="utf-8")
        schema = json.loads((SCHEMAS / "insight_llm_output.schema.json").read_text(encoding="utf-8"))
        line = next(line for line in template.splitlines() if "Ответ — объект ровно с" in line)
        tail = template[template.index(line):template.index(line) + 400]
        keys = r"\b(title_ru|description_ru|advantage_ru|case_example|explanation_ru|source_summaries)\b"
        listed = re.findall(keys, tail)
        self.assertEqual(list(dict.fromkeys(listed)), list(schema["properties"]))
        self.assertIn("шестью", line)
        self.assertEqual(len(schema["properties"]), 6)


class GenerationEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """E1 на границе сервиса: корректный ответ модели со ссылками [doc:3]–[doc:5] принимается."""

    async def test_answer_with_high_doc_refs_is_generated_not_fallback(self) -> None:
        evidence = EVIDENCE[:5]
        answer = {
            "title_ru": "Нейроморфные прототипы для периферийных устройств",
            "description_ru": "Исследователи показали прототип [doc:3], пилот снизил энергопотребление [doc:4].",
            "advantage_ru": "Снижение энергопотребления на 45 процентов по данным источника [doc:5].",
            "case_example": {"text_ru": "Пилотное внедрение прототипа описано в источнике [doc:4].",
                             "document_id": evidence[3].document_id},
            "explanation_ru": (
                "Модель отнесла наблюдение к слабому сигналу: лексика ранней стадии равна 0.82, "
                "а доля доверенных источников составляет 0.60."
            ),
            "source_summaries": [
                {"document_id": document.document_id, "summary_ru": "Исследователи описали прототип и пилот."}
                for document in evidence
            ],
        }
        provider = FakeProvider(responses=[json.dumps(answer, ensure_ascii=False)])
        use_case = GenerateInsight(
            build_chain([provider]), PromptBuilder(PROMPTS, SCHEMAS), InMemoryInsightRepository(),
            FakeClock(), GenerateConfig(max_attempts=1),
        )
        command = GenerateInsightCommand(
            idempotency_key="job-q:cand-q:insight:insight_v1", candidate=CANDIDATE, evidence=evidence
        )
        insight = await use_case.execute(command)
        self.assertIs(insight.status, InsightStatus.GENERATED, insight.grounding.summary)
        self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main()