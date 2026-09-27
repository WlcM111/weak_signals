"""Отказ CDN arXiv: 406 без узла google в via помечается отдельной причиной, заголовки сохраняются."""

from __future__ import annotations

import unittest

from collector.adapters.outbound.sources.arxiv import _as_rate_limit
from collector.application.ports import HttpStatusError
from collector.domain.errors import AdapterFailure
from collector.domain.values import AdapterErrorCode


class ArxivCdnTest(unittest.TestCase):
    def test_edge_406_is_marked_as_cdn_tls_rejection(self) -> None:
        failure = AdapterFailure(AdapterErrorCode.HTTP_4XX.value, "источник ответил кодом 406 "
                                 "[via=1.1 varnish, 1.1 varnish; x-cache=MISS, MISS; x-served-by=cache-lga21]")
        result = _as_rate_limit(failure)
        self.assertEqual(result.code, AdapterErrorCode.HTTP_4XX.value)
        self.assertIn("CDN arXiv отклонил TLS-клиент", result.message)
        self.assertIn("x-served-by=cache-lga21", result.message)

    def test_origin_406_keeps_previous_meaning(self) -> None:
        failure = AdapterFailure(AdapterErrorCode.HTTP_4XX.value, "источник ответил кодом 406 [via=1.1 google, 1.1 varnish]")
        self.assertNotIn("CDN arXiv", _as_rate_limit(failure).message)
        self.assertNotIn("CDN arXiv", _as_rate_limit(AdapterFailure(AdapterErrorCode.HTTP_4XX.value,
                                                                    "источник ответил кодом 406")).message)

    def test_search_query_uses_significant_words(self) -> None:
        from collector.adapters.outbound.sources.arxiv import search_query

        self.assertEqual(search_query("photonic FMCW lidar on silicon chip"),
                         "all:photonic AND all:FMCW AND all:lidar AND all:silicon")
        self.assertEqual(search_query("perovskite"), 'all:"perovskite"')
        self.assertEqual(search_query("AI for the edge"), 'all:"AI for the edge"')

    def test_status_error_keeps_cdn_headers(self) -> None:
        error = HttpStatusError(406, "источник ответил кодом 406", None, {"via": "1.1 varnish"})
        self.assertEqual((error.status, error.cdn_headers), (406, {"via": "1.1 varnish"}))
        self.assertEqual(HttpStatusError(500, "x").cdn_headers, {})


if __name__ == "__main__":
    unittest.main()
