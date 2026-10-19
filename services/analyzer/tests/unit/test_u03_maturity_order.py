"""U-03: зрелость проверяется раньше достаточности источников; общие уточнения безопасности не делают зрелую область
новой технологией, но конкретный новый механизм остаётся кандидатом."""

from __future__ import annotations

import unittest

from analyzer.domain.rules import RuleThresholds, apply_exclusion_rules, mature_field
from analyzer.domain.values import Decision, DecisionReason, TrustLevel

from ..fakes import make_document
from .test_rules import features

THRESHOLDS = RuleThresholds()
SINGLE_MEDIUM = [make_document(1, "A", trust_level=TrustLevel.MEDIUM)]  # срабатывает «один источник»


class MatureBeforeEvidenceTest(unittest.TestCase):
    def test_mature_single_source_is_mature_with_evidence_note(self) -> None:
        result = apply_exclusion_rules(features(), SINGLE_MEDIUM, THRESHOLDS, titles=("Prompt Injection Attacks",))
        self.assertIs(result.decision, Decision.MATURE)
        self.assertIs(result.reason, DecisionReason.MATURITY_LEXICON)
        self.assertIn("Также:", result.explanation_ru)  # причина про источник не потеряна, решение одно

    def test_non_mature_single_source_still_insufficient(self) -> None:
        result = apply_exclusion_rules(features(), SINGLE_MEDIUM, THRESHOLDS, titles=("ClawLess security model",))
        self.assertIs(result.reason, DecisionReason.SINGLE_SOURCE)

    def test_off_topic_keeps_priority(self) -> None:
        result = apply_exclusion_rules(features(emb_sim_query=0.29), SINGLE_MEDIUM, THRESHOLDS,
                                       titles=("Prompt Injection Attacks",))
        self.assertIs(result.decision, Decision.OFF_TOPIC)


class SecurityQualifiersTest(unittest.TestCase):
    def test_generic_security_words_name_the_mature_field(self) -> None:
        for title in ("Prompt Injection Attacks", "Prompt injection defenses", "Adversarial attack detection",
                      "Fraud Detection", "Anomaly detection and prevention"):
            self.assertIsNotNone(mature_field(title), title)

    def test_new_mechanism_on_mature_base_is_not_mature(self) -> None:
        for title in ("CaMeL capability-based defense against prompt injection",
                      "Tool poisoning detection for MCP servers", "Adversarial attack detection for LLM agents"):
            self.assertIsNone(mature_field(title), title)


if __name__ == "__main__":
    unittest.main()
