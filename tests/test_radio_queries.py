"""Targeted radio discovery input (Phase 1): query generation + verification.

Everything here runs against injected in-memory fetchers - NO real SerpAPI
request, NO real API key, NO network. Covers:

- ``discovery.radio.queries.build_radio_queries``: deterministic,
  submission-targeted, deduplicated, capped; submission/intent, role and
  geography phrasings.
- The public ``DiscoveryRequest`` contract does NOT accept ``submission_intent``
  (unknown-field rejection, matching the deployed API contract).
- A small controlled discovery run: with a target-typed job, only the real
  station page becomes a stored station; junk candidates are never fetched
  and nothing unreviewed leaks into listings.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

from discovery.models import DiscoveryRequest
from discovery.radio.jobs import run_radio_discovery_job
from discovery.radio.queries import MAX_QUERIES, build_radio_queries

from database.service import PersistenceService


def _req(**overrides) -> DiscoveryRequest:
    fields = {"query": "community radio", "limit": 25}
    fields.update(overrides)
    return DiscoveryRequest(**fields)


class RadioQueryBuilderTests(unittest.TestCase):
    """The submitted query text is derived from the request dimensions."""

    def test_default_queries_carry_submission_intent_first(self):
        qs = build_radio_queries(_req(station_type="community"))
        self.assertEqual(qs[0], "community radio station music submissions")
        self.assertIn("community radio station accepting music submissions", qs)

    def test_role_phrasings_always_covered(self):
        qs = build_radio_queries(_req(station_type="community"))
        joined = " | ".join(qs)
        self.assertIn(" music director ", f" {joined} ")
        self.assertIn(" program director ", f" {joined} ")
        self.assertIn(" music programming ", f" {joined} ")

    def test_all_intent_phrasings_covered(self):
        qs = build_radio_queries(_req(station_type="community"))
        joined = " | ".join(qs)
        for phrase in ("music submissions", "submit music",
                       "artist submissions", "music submission"):
            self.assertIn(phrase, joined)

    def test_most_specific_subject_first_when_typed_and_genre(self):
        qs = build_radio_queries(_req(station_type="college", genre="jazz"))
        self.assertEqual(qs[0], "jazz college radio station music submissions")
        self.assertTrue(any(q.startswith("college radio station ") for q in qs))
        self.assertTrue(any(q.startswith("jazz radio station ") for q in qs))

    def test_request_query_is_subject_fallback_without_dimensions(self):
        qs = build_radio_queries(DiscoveryRequest("KQXR independent radio"))
        self.assertEqual(qs[0], "kqxr independent radio music submissions")

    def test_geography_appended_after_intent(self):
        qs = build_radio_queries(_req(
            station_type="community", city="Los Angeles",
            state_or_region="California", country="United States"))
        self.assertEqual(
            qs[0],
            "community radio station music submissions "
            "Los Angeles California United States")

    def test_capped_deduped_and_deterministic(self):
        qs1 = build_radio_queries(_req(
            station_type="college", genre="jazz",
            city="Austin", state_or_region="Texas", country="United States"))
        self.assertLessEqual(len(qs1), MAX_QUERIES)
        self.assertEqual(len(qs1), len(set(qs1)))
        self.assertEqual(qs1, build_radio_queries(_req(
            station_type="college", genre="jazz",
            city="Austin", state_or_region="Texas", country="United States")))


class RadioRequestContractTests(unittest.TestCase):
    """The public ``DiscoveryRequest`` contract matches the deployed API:
    ``submission_intent`` is NOT an accepted request field; rejection must
    read exactly like the live 400 the API returns.
    """

    def test_supported_fields_round_trip_via_dict(self):
        req = DiscoveryRequest.from_dict({
            "query": "community radio", "station_type": "community",
            "country": "United States", "limit": 25})
        self.assertEqual(req.station_type, "community")
        self.assertEqual(req.country, "United States")
        self.assertNotIn("submission_intent", req.to_dict())

    def test_submission_intent_is_rejected_as_unknown_field(self):
        with self.assertRaises(ValueError) as ctx:
            DiscoveryRequest.from_dict(
                {"query": "community radio",
                 "submission_intent": "music submissions"})
        self.assertIn("unknown request fields: ['submission_intent']",
                      str(ctx.exception))


class RadioTargetedJobVerificationTests(unittest.TestCase):
    """A small controlled discovery run proves the input fix end to end.

    The mocked SerpAPI page set contains exactly one real station (KQXR) plus
    junk (playlist / tickets / news). With a targeted job, the provider query
    text must be the targeted builder output, only the station page may be
    fetched, and the store must end with exactly that station.
    """

    def setUp(self):
        from tests.test_station_research import (
            SERPAPI_API_KEY_ENV,
            SERPAPI_KEY,
            _Web,
            _station_pages,
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = PersistenceService(
            os.path.join(self._tmp.name, "mie.db"))
        self.web = _Web(pages=_station_pages())
        self._env = mock.patch.dict(
            os.environ, {SERPAPI_API_KEY_ENV: SERPAPI_KEY}, clear=True)
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.repo.close()
        self._tmp.cleanup()

    @staticmethod
    def _search_queries(web) -> list[str]:
        queries: list[str] = []
        for url in web.calls:
            if "engine=google" not in url:
                continue
            parsed = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query)
            queries.extend(parsed.get("q", []))
        return queries

    def test_targeted_job_drives_targeted_queries(self):
        config = {"query": "community radio",
                  "station_type": "community",
                  "limit": 25}
        report = run_radio_discovery_job(self.repo, config, fetcher=self.web)

        expected = set(build_radio_queries(_req(station_type="community")))
        issued = set(self._search_queries(self.web))
        self.assertTrue(expected.issubset(issued))
        self.assertTrue(issued.issubset(expected))
        self.assertTrue(any("community radio station music submissions" == q
                            for q in issued))
        self.assertIn("candidates_found", report)
        self.assertGreaterEqual(report["candidates_found"], 1)

    def test_only_real_station_becomes_stored_and_listed(self):
        config = {"query": "community radio",
                  "station_type": "community"}
        report = run_radio_discovery_job(self.repo, config, fetcher=self.web)
        self.assertGreaterEqual(report["records_ingested"], 1)

        station = self.repo.get_station("domain:kqxr.example")
        self.assertIsNotNone(station)
        self.assertTrue(station["name"])

        for junk in ("spotify", "tickets.example", "news.example"):
            self.assertFalse(any(junk in u for u in self.web.calls),
                             f"{junk} must never be fetched")

        rows, total = self.repo.list_stations(limit=50)
        self.assertEqual(total, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["website"], "https://kqxr.example/")

    def test_targeted_job_merges_without_duplicating(self):
        config = {"query": "community radio",
                  "station_type": "community"}
        run_radio_discovery_job(self.repo, config, fetcher=self.web)
        report = run_radio_discovery_job(self.repo, config, fetcher=self.web)
        self.assertGreaterEqual(report["duplicates"], 1)
        _, total = self.repo.list_stations(limit=50)
        self.assertEqual(total, 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()