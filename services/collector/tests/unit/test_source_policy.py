"""Бюджет запросов и паузы по источникам (этап 3, B2-b)."""

from __future__ import annotations

import unittest

from collector.domain.source_policy import DEFAULT_POLICIES, SourcePolicy, parse_source_policies
from collector.domain.values import SourceKey


class SourcePolicyTest(unittest.TestCase):
    def test_defaults_and_overrides(self) -> None:
        policies = parse_source_policies("semantic_scholar:requests=4,pause=20;openalex:retries=1")
        self.assertEqual(policies[SourceKey.SEMANTIC_SCHOLAR], SourcePolicy(max_requests=4, rate_limit_retries=2,
                                                                            rate_limit_pause=20.0))
        # этап 4.7: у OpenAlex по умолчанию бюджет 40 запросов на сбор, переопределение повторов его сохраняет
        self.assertEqual(policies[SourceKey.OPENALEX], SourcePolicy(max_requests=60, rate_limit_retries=1))
        self.assertEqual(policies[SourceKey.GDELT], DEFAULT_POLICIES[SourceKey.GDELT])
        self.assertEqual(parse_source_policies(""), DEFAULT_POLICIES)

    def test_invalid_values_are_rejected(self) -> None:
        for text in ("unknown:requests=1", "gdelt:speed=1", "gdelt:requests=", "gdelt:retries=9", "gdelt:pause=-1"):
            with self.assertRaises(ValueError, msg=text):
                parse_source_policies(text)


if __name__ == "__main__":
    unittest.main()
