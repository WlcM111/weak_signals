"""Настройки этапов 5.4–5.8 доходят до задания: `_narrate_config` перечисляет поля явно, и пропущенное поле молча
получало значение по умолчанию (так было с final_mature_* в исходном разборе другой команды). Каждое поле
заполняется значением, отличным от умолчания, — поэтому тест ловит пропуск любого поля, а не только перечисленных."""

from __future__ import annotations

import dataclasses
import unittest

from orchestrator.application.stages.narrate import NarrateConfig
from orchestrator.application.use_cases.run_job import _narrate_config


def _other(value: object) -> object:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    if isinstance(value, float):
        return value + 0.125
    if isinstance(value, str):
        return value + "_x"
    if isinstance(value, tuple):
        return (*value, "x")
    return value


class ConfigForwardingTest(unittest.TestCase):
    def test_every_narrate_setting_reaches_the_job(self) -> None:
        defaults = NarrateConfig()
        changed = {f.name: _other(getattr(defaults, f.name)) for f in dataclasses.fields(NarrateConfig)
                   if f.name not in ("top_n", "llm_allowed")}
        base = NarrateConfig(**changed)
        built = _narrate_config(base, top_n=7, llm_allowed=None)
        self.assertEqual(built.top_n, 7)
        for name, value in changed.items():
            if value is getattr(defaults, name):  # тип без «другого» значения (например, None) — не проверяется
                continue
            self.assertEqual(getattr(built, name), value, name)


if __name__ == "__main__":
    unittest.main()
