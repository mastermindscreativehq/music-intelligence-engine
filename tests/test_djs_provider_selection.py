"""Focused, MOCKED integration tests for DJ-discovery provider SELECTION.

The ``select_djs_discovery_provider`` seam is the single deterministic
decision point used by BOTH route twins (threaded ``routes.py`` and FastAPI
``app.py``). These tests prove selection is a pure function of
*configuration*: a configured ``SERPAPI_API_KEY`` selects the dedicated
SerpApiSearchDjsProvider; otherwise the preserved generic HTTP seam is used;
an absent configuration yields the honest unconfigured provider (route 503).
Nothing is ever fabricated and no real key / no network is required.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from discovery.djs.selector import select_djs_discovery_provider
from discovery.djs.serpapi_provider import (
    SERPAPI_API_KEY_ENV,
    SERPAPI_BASE_URL_ENV,
    SerpApiSearchDjsProvider,
)
from discovery.djs.http_provider import HttpSearchDjsProvider


def _clear_env():
    for name in (SERPAPI_API_KEY_ENV, SERPAPI_BASE_URL_ENV):
        os.environ.pop(name, None)


class DjsProviderSelectionTests(unittest.TestCase):

    def setUp(self):
        _clear_env()

    def tearDown(self):
        _clear_env()

    def test_serpapi_key_selects_dedicated_provider(self):
        os.environ[SERPAPI_API_KEY_ENV] = "dummy-key-for-selection-test"
        provider = select_djs_discovery_provider()
        self.assertIsInstance(provider, SerpApiSearchDjsProvider)
        self.assertTrue(provider.configured)

    def test_serpapi_base_url_alone_is_not_enough(self):
        os.environ[SERPAPI_BASE_URL_ENV] = "https://serpapi.com/search"
        provider = select_djs_discovery_provider()
        self.assertNotIsInstance(provider, SerpApiSearchDjsProvider)
        self.assertIsInstance(provider, HttpSearchDjsProvider)
        self.assertFalse(provider.configured)

    def test_absent_configuration_is_honest_unconfigured(self):
        _clear_env()
        provider = select_djs_discovery_provider()
        self.assertIsInstance(provider, HttpSearchDjsProvider)
        self.assertFalse(provider.configured)

    def test_selection_never_seeds_a_fabricated_provider(self):
        _clear_env()
        provider = select_djs_discovery_provider()
        self.assertFalse(provider.configured)
        self.assertNotEqual(type(provider).__name__, "DjsSeedListProvider")

    def test_cleared_key_falls_back_to_unconfigured(self):
        os.environ[SERPAPI_API_KEY_ENV] = "dummy-key-for-selection-test"
        os.environ.pop(SERPAPI_API_KEY_ENV)
        provider = select_djs_discovery_provider()
        self.assertFalse(provider.configured)

    def test_selection_requires_no_network_and_no_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            provider = select_djs_discovery_provider(fetcher=None)
        self.assertFalse(provider.configured)


if __name__ == "__main__":
    unittest.main()