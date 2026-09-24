"""Реестр провайдеров ТЗ и политика адресов endpoint."""

from __future__ import annotations

import unittest

from insight.domain import catalog
from insight.domain.endpoint_policy import check


class CatalogTest(unittest.TestCase):
    """Состав реестра соответствует §3.1 ТЗ: только разрешённые компании и модели."""

    EXPECTED = {
        "gigachat": {"GigaChat-2", "GigaChat-2-Pro", "GigaChat-2-Max"},
        "yandexgpt": {"yandexgpt-lite/latest", "yandexgpt/latest", "yandexgpt/rc"},
        "qwen": {"qwen3.6-35b-a3b", "qwen3-235b"},
        "openai": {"gpt-4.1", "gpt-5.6-luna"},
    }

    def test_all_allowed_models_present(self) -> None:
        for provider_id, models in self.EXPECTED.items():
            with self.subTest(provider=provider_id):
                self.assertEqual(set(catalog.provider(provider_id).model_ids), models)

    def test_unknown_model_rejected(self) -> None:
        self.assertFalse(catalog.is_allowed("openai", "gpt-5-turbo-ultra"))
        self.assertFalse(catalog.is_allowed("anthropic", "claude"))
        self.assertFalse(catalog.is_allowed("openrouter", "gpt-4.1"))

    def test_protocol_families_differ(self) -> None:
        protocols = {spec.provider_id: spec.protocol.value for spec in catalog.PROVIDERS}
        self.assertEqual(protocols["gigachat"], "gigachat_sdk")
        self.assertEqual(protocols["yandexgpt"], "yandex_openai")
        self.assertEqual(protocols["openai"], "openai_chat")
        self.assertNotEqual(protocols["gigachat"], protocols["openai"])

    def test_fake_not_in_approved_without_test_env(self) -> None:
        self.assertNotIn("fake", catalog.APPROVED_PROVIDER_IDS)

    def test_catalog_rows_have_required_fields(self) -> None:
        for row in catalog.catalog_rows():
            for key in ("provider_id", "model_id", "protocol", "context_tokens", "capabilities"):
                self.assertIn(key, row)


class EndpointPolicyTest(unittest.TestCase):
    """Адрес провайдера проверяется как недоверенный вход."""

    def test_official_https_allowed(self) -> None:
        verdict = check("https://api.openai.com/v1")
        self.assertTrue(verdict.allowed, verdict.reason_ru)
        self.assertEqual(verdict.port, 443)

    def test_plain_http_rejected(self) -> None:
        self.assertFalse(check("http://api.openai.com/v1").allowed)

    def test_loopback_rejected(self) -> None:
        self.assertFalse(check("https://127.0.0.1:443/v1").allowed)

    def test_private_network_rejected(self) -> None:
        self.assertFalse(check("https://10.1.2.3/v1").allowed)
        self.assertFalse(check("https://192.168.0.10:8443/v1").allowed)

    def test_cloud_metadata_rejected(self) -> None:
        self.assertFalse(check("https://169.254.169.254/latest/meta-data").allowed)

    def test_exotic_port_rejected(self) -> None:
        self.assertFalse(check("https://api.example.org:1337/v1").allowed)

    def test_file_scheme_rejected(self) -> None:
        self.assertFalse(check("file:///etc/passwd").allowed)

    def test_trusted_local_service_allowed(self) -> None:
        verdict = check("http://local-llm:8000/v1", trusted_local=True)
        self.assertTrue(verdict.allowed, verdict.reason_ru)

    def test_trusted_local_does_not_open_arbitrary_hosts(self) -> None:
        self.assertFalse(check("http://evil.example.org/v1", trusted_local=True).allowed)

    def test_ip_literal_requires_sni_hint(self) -> None:
        verdict = check("https://93.184.216.34:443/v1")
        self.assertTrue(verdict.allowed, verdict.reason_ru)
        self.assertTrue(verdict.needs_sni_hint)

    def test_empty_url_rejected(self) -> None:
        self.assertFalse(check("").allowed)


if __name__ == "__main__":
    unittest.main()