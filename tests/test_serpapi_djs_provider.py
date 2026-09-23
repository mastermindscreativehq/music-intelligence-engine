"""Focused, MOCKED tests for the dedicated SerpAPI DJ search provider.

Everything here runs against an injected in-memory fetcher double — NO real
SerpAPI request, NO real API key, NO network. Every test is explicitly a
mocked test (requirement: mock the HTTP response; never require a real
SerpAPI key). No live SerpAPI discovery claim is made anywhere.
"""

from __future__ import annotations

import json
import re
import unittest

from discovery.djs.serpapi_provider import (
    SerpApiSearchDjsProvider,
    load_serpapi_search_entries,
)
from discovery.models import (
    Candidate,
    DiscoveryRequest,
    SourceType,
)
from discovery.djs.http_provider import DiscoveryProviderNotConfigured


class _FetchResult:
    """FetchResult double exposing the ``.ok`` / ``.body`` seam."""

    def __init__(self, body: str | None = None, *, ok: bool = True,
                 error_kind: str | None = None,
                 error_message: str | None = None) -> None:
        self.ok = ok
        self.body = body
        self.error_kind = error_kind
        self.error_message = error_message
        self.final_url = None

    def __getattr__(self, name):  # tolerate extra attribute probes
        raise AttributeError(name)


class _FakeSerpApiFetcher:
    """URL-keyed fetcher double. URL carries the ONLY place the key lives."""

    def __init__(self, pages: dict[str, _FetchResult] | None = None) -> None:
        self.pages = dict(pages or {})
        self.calls: list[str] = []

    def fetch(self, url: str, *, timeout=None):
        self.calls.append(url)
        return self.pages.get(url, _FetchResult(ok=False))


def _result(**overrides):
    payload = {
        "organic_results": [
            {"position": 1, "title": "Bella Afrobeats — NY DJ",
             "link": "https://bella.example",
             "snippet": "NYC-based Afrobeats DJ for weddings and clubs."},
            {"position": 2, "title": "Kofi B2B | Party DJ",
             "link": "https://kofi.example",
             "snippet": "Booking-friendly collective DJ from Ghana."},
        ]
    }
    payload.update(overrides)
    return _FetchResult(body=json.dumps(payload))


def _request(**overrides):
    fields = {"query": "DJ", "limit": 50}
    fields.update(overrides)
    return DiscoveryRequest(**fields)


class SerpApiSearchDjsProviderTests(unittest.TestCase):

    def _provider(self, *, pages=None, base_url=None, api_key="K-TOKEN",
                  fetcher=None):
        fetcher = fetcher or _FakeSerpApiFetcher(pages or {})
        provider = SerpApiSearchDjsProvider(
            base_url=base_url if base_url is not None
            else "https://serpapi.com/search",
            api_key=api_key, fetcher=fetcher)
        return provider, fetcher

    # -- serpapi seam: configured / honest unconfigured -----------------------

    def test_configured_true_with_base_and_key(self):
        provider, _ = self._provider()
        self.assertTrue(provider.configured)

    def test_configured_false_without_api_key(self):
        provider, _ = self._provider(api_key="")
        self.assertFalse(provider.configured)

    def test_configured_false_without_base_url(self):
        provider, _ = self._provider(base_url="", api_key="K-TOKEN")
        self.assertFalse(provider.configured)

    def test_search_raises_when_not_configured(self):
        """Missing key must raise — never fabricate, never fake a result."""
        provider, _ = self._provider(api_key="", pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=":
            _result()})
        with self.assertRaises(DiscoveryProviderNotConfigured):
            provider.search(_request(), ["DJ"])

    # -- successful response --------------------------------------------------

    def test_success_maps_title_url_snippet_and_provenance(self):
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _result()})
        out = provider.search(_request(), ["DJ"])
        self.assertEqual(len(out), 2)
        first = out[0]
        self.assertEqual(first.title, "Bella Afrobeats — NY DJ")
        self.assertEqual(first.url, "https://bella.example")
        self.assertEqual(first.snippet,
                         "NYC-based Afrobeats DJ for weddings and clubs.")
        self.assertEqual(first.source, "serpapi_google")
        self.assertEqual(first.source_type, SourceType.SEARCH_SOURCE)

    # -- organic_results parsing / URL extraction -----------------------------

    def test_organic_results_parsing(self):
        payload = {"organic_results": [
            {"title": "A", "link": "https://a.example"},
            {"title": "B", "link": "https://b.example"},
        ]}
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _FetchResult(body=json.dumps(payload))})
        urls = [c.url for c in provider.search(_request(), ["DJ"])]
        self.assertEqual(urls, ["https://a.example", "https://b.example"])

    def test_organic_result_without_link_is_ignored(self):
        payload = {"organic_results": [
            {"title": "No URL"},
            {"title": "OK", "link": "https://ok.example"},
        ]}
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _FetchResult(body=json.dumps(payload))})
        out = provider.search(_request(), ["DJ"])
        self.assertEqual([c.url for c in out], ["https://ok.example"])

    def test_empty_organic_results_yields_nothing(self):
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _FetchResult(body=json.dumps({"organic_results": []}))})
        self.assertEqual(provider.search(_request(), ["DJ"]), [])

    # -- snippet extraction ---------------------------------------------------

    def test_snippet_carried_when_present_else_empty(self):
        payload = {"organic_results": [
            {"title": "With snip", "link": "https://s.example",
             "snippet": "   Amsterdam   tech house DJ.  "},
            {"title": "No snip", "link": "https://n.example"},
        ]}
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _FetchResult(body=json.dumps(payload))})
        out = provider.search(_request(), ["DJ"])
        self.assertEqual(out[0].snippet, "Amsterdam tech house DJ.")
        self.assertEqual(out[1].snippet, "")

    # -- limit enforcement / dedup --------------------------------------------

    def test_limit_enforced(self):
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _result()})
        out = provider.search(_request(limit=1), ["DJ"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].url, "https://bella.example")

    def test_duplicate_urls_removed(self):
        payload = {"organic_results": [
            {"title": "A", "link": "https://dup.example"},
            {"title": "B", "link": "https://dup.example"},
            {"title": "C", "link": "https://c.example"},
        ]}
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _FetchResult(body=json.dumps(payload))})
        urls = [c.url for c in provider.search(_request(), ["DJ"])]
        self.assertEqual(urls, ["https://dup.example", "https://c.example"])

    # -- honest error handling ------------------------------------------------

    def test_malformed_response_ignored(self):
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _FetchResult(body="not json{{")})
        self.assertEqual(provider.search(_request(), ["DJ"]), [])

    def test_serpapi_error_body_treated_as_failure_not_fabrication(self):
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _FetchResult(body=json.dumps({"error": "Key is missing"}))})
        self.assertEqual(provider.search(_request(), ["DJ"]), [])

    # -- the API key must never leak ------------------------------------------

    def test_api_key_not_in_candidates(self):
        provider, _ = self._provider(pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=K-TOKEN":
            _result()})
        out = provider.search(_request(), ["DJ"])
        for candidate in out:
            for value in candidate.to_dict().values():
                if isinstance(value, str):
                    self.assertNotIn("K-TOKEN", value)

    def test_api_key_never_logged_in_error(self):
        provider, _ = self._provider(api_key="TOP-SECRET-KEY", pages={
            "https://serpapi.com/search?engine=google&q=DJ&api_key=TOP-SECRET-KEY":
            _FetchResult(ok=False, error_kind="http_status",
                         error_message="HTTP 429")})
        out = provider.search(_request(), ["DJ"])
        self.assertEqual(out, [])
        self.assertEqual(len(provider.failures), 1)
        self.assertNotIn("TOP-SECRET-KEY", provider.failures[0].message)


class SerpApiPartialTransportFailureTests(unittest.TestCase):
    """Per-query SerpAPI transport failures are recorded, never an abort."""

    BASE = "https://serpapi.com/search"

    def _url(self, query: str) -> str:
        return f"{self.BASE}?engine=google&q={query}&api_key=K-TOKEN"

    def test_failed_query_recorded_and_remaining_queries_continue(self):
        first, second, third = self._url("FIRST"), self._url("SECOND"), \
            self._url("THIRD")
        third_payload = {"organic_results": [
            {"title": "A Different DJ", "link": "https://third.example"},
        ]}
        provider = SerpApiSearchDjsProvider(
            base_url=self.BASE, api_key="K-TOKEN",
            fetcher=_FakeSerpApiFetcher({
                first: _FetchResult(ok=False, error_kind="http_status",
                                    error_message="HTTP 429"),
                second: _result(),
                third: _FetchResult(body=json.dumps(third_payload)),
            }))
        out = provider.search(
            _request(limit=50), ["FIRST", "SECOND", "THIRD"])
        self.assertEqual([c.url for c in out],
                         ["https://bella.example", "https://kofi.example",
                          "https://third.example"])
        self.assertEqual(len(provider.failures), 1)
        failure = provider.failures[0]
        self.assertEqual(failure.stage, "provider")
        self.assertEqual(failure.error_kind, "http_status")
        self.assertIn("FIRST", failure.message)
        self.assertNotIn("K-TOKEN", failure.message)

    def test_all_queries_failing_is_recorded_not_an_abort(self):
        provider = SerpApiSearchDjsProvider(
            base_url=self.BASE, api_key="K-TOKEN",
            fetcher=_FakeSerpApiFetcher({}))
        out = provider.search(
            _request(limit=50), ["FIRST", "SECOND", "THIRD"])
        self.assertEqual(out, [])
        self.assertEqual(len(provider.failures), 3)
        self.assertEqual(provider.failures[0].error_kind, "provider_error")
        self.assertNotIn("K-TOKEN",
                         " | ".join(f.message for f in provider.failures))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
