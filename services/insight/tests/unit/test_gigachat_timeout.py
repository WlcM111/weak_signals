"""Таймаут из настроек доходит до SDK GigaChat (стенд 29.09: без него SDK обрывал ответы с ReadTimeout)."""

import os
import sys
import types
import unittest

from insight.adapters.outbound.llm.gigachat_provider import GigaChatProvider


class _SdkWithTimeout:
    last: dict = {}

    def __init__(self, credentials=None, scope=None, model=None, ca_bundle_file=None,  # noqa: ANN001
                 verify_ssl_certs=True, timeout=None) -> None:  # noqa: ANN001
        type(self).last = {"model": model, "timeout": timeout}


class _SdkWithoutTimeout:
    def __init__(self, credentials=None, scope=None, model=None, ca_bundle_file=None,  # noqa: ANN001
                 verify_ssl_certs=True) -> None:  # noqa: ANN001
        pass


class GigaChatTimeoutTest(unittest.TestCase):
    def _provider_with(self, sdk: type) -> GigaChatProvider:
        module = types.ModuleType("gigachat")
        module.GigaChat = sdk  # type: ignore[attr-defined]
        sys.modules["gigachat"] = module
        self.addCleanup(sys.modules.pop, "gigachat", None)
        return GigaChatProvider(credentials="x", model="GigaChat-2-Max", scope="GIGACHAT_API_PERS",
                                timeout_seconds=300.0)

    def test_timeout_is_passed_to_sdk(self) -> None:
        self._provider_with(_SdkWithTimeout)._sdk()
        self.assertEqual(_SdkWithTimeout.last, {"model": "GigaChat-2-Max", "timeout": 300.0})

    def test_timeout_goes_to_environment_when_sdk_has_no_parameter(self) -> None:
        previous = os.environ.pop("GIGACHAT_TIMEOUT", None)
        self.addCleanup(lambda: os.environ.pop("GIGACHAT_TIMEOUT", None) if previous is None
                        else os.environ.__setitem__("GIGACHAT_TIMEOUT", previous))
        self._provider_with(_SdkWithoutTimeout)._sdk()
        self.assertEqual(os.environ["GIGACHAT_TIMEOUT"], "300.0")


if __name__ == "__main__":
    unittest.main()
