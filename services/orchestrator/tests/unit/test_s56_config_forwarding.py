"""Настройки этапов 5.4–5.6 доходят до задания: `_narrate_config` перечисляет поля явно, и пропущенное поле молча
получало значение по умолчанию (так было с final_mature_* в исходном разборе другой команды)."""

from __future__ import annotations

import dataclasses
import unittest

from orchestrator.application.stages.narrate import NarrateConfig
from orchestrator.application.use_cases.run_job import _narrate_config


class ConfigForwardingTest(unittest.TestCase):
    def test_every_narrate_setting_reaches_the_job(self) -> None:
        base = NarrateConfig(maturity_filter_enabled=True, final_mature_research_only=False,
                             final_mature_min_confidence=0.7, rank_model_first_share=0.25,
                             rubric_pool_research_singles=False)
        built = _narrate_config(base, top_n=7, llm_allowed=None)
        self.assertEqual(built.top_n, 7)
        for field in dataclasses.fields(NarrateConfig):
            if field.name in ("top_n", "llm_allowed"):
                continue
            self.assertEqual(getattr(built, field.name), getattr(base, field.name), field.name)


if __name__ == "__main__":
    unittest.main()
