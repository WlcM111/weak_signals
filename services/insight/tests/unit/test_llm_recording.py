"""Запись промптов и ответов LLM (этап 2, режим А; решение владельца 03.10.2026)."""

from __future__ import annotations

import unittest

from insight.adapters.outbound.llm.fake_provider import FakeProvider
from insight.application.dto import LLMMessage
from insight.application.llm_recording import replay_key
from insight.application.provider_chain import ChainConfig, ProviderChain
from insight.domain.errors import ProviderTimeout
from insight.domain.values import Purpose
from ws_common.ids import set_correlation_id

from ..fakes import FakeClock, InMemoryCallLog


class _Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.entries: list = []
        self.fail = fail

    async def record(self, entry) -> None:  # noqa: ANN001
        if self.fail:
            raise RuntimeError("база недоступна")
        self.entries.append(entry)


MESSAGES = [LLMMessage("system", "Ты аналитик."), LLMMessage("user", '{"items": [{"id": "c1"}]}')]


class RecordingTest(unittest.IsolatedAsyncioTestCase):
    def chain(self, providers, recorder) -> ProviderChain:  # noqa: ANN001
        return ProviderChain(providers, InMemoryCallLog(), FakeClock(), ChainConfig(temperature=0.2), recorder=recorder)

    async def test_success_and_fallback_are_recorded_with_replay_key(self) -> None:
        set_correlation_id("job-1")
        recorder = _Recorder()
        primary = FakeProvider(name_value="gigachat", errors=[ProviderTimeout("медленно", "gigachat")])
        secondary = FakeProvider(name_value="yandexgpt", responses=['{"ok": true}'])
        outcome = await self.chain([primary, secondary], recorder).complete(
            MESSAGES, json_schema={"type": "object"}, max_tokens=500, purpose=list(Purpose)[0], prompt_version="judge_v6",
            request_sha256="abc")
        self.assertEqual(outcome.provider, "yandexgpt")
        self.assertEqual([(e.provider, e.status) for e in recorder.entries], [("gigachat", "TIMEOUT"), ("yandexgpt", "OK")])
        key = replay_key(MESSAGES, {"type": "object"}, 500, 0.2)
        self.assertTrue(all(e.replay_key == key for e in recorder.entries))
        self.assertEqual(recorder.entries[1].response_text, '{"ok": true}')
        self.assertEqual(recorder.entries[0].error_code, "PROVIDER_TIMEOUT")
        self.assertEqual({e.correlation_id for e in recorder.entries}, {"job-1"})
        self.assertEqual(recorder.entries[1].messages[1], ("user", MESSAGES[1].content))

    async def test_recorder_failure_keeps_answer_and_is_counted(self) -> None:
        chain = self.chain([FakeProvider(name_value="gigachat", responses=["{}"])], _Recorder(fail=True))
        outcome = await chain.complete(MESSAGES, json_schema=None, max_tokens=100, purpose=list(Purpose)[0],
                                       prompt_version="v", request_sha256="x")
        self.assertEqual(outcome.result.text, "{}")
        self.assertEqual(chain.recording_failures, 1)

    def test_replay_key_depends_on_request_only(self) -> None:
        base = replay_key(MESSAGES, None, 100, 0.2)
        self.assertEqual(base, replay_key([("system", "Ты аналитик."), ("user", MESSAGES[1].content)], None, 100, 0.2))
        self.assertNotEqual(base, replay_key(MESSAGES, None, 101, 0.2))
        self.assertNotEqual(base, replay_key(MESSAGES, {"type": "object"}, 100, 0.2))
        self.assertNotEqual(base, replay_key(MESSAGES[:1], None, 100, 0.2))


if __name__ == "__main__":
    unittest.main()
