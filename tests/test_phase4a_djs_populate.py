"""DJ pipeline independence from radio stations (architecture correction).

The DJ pipeline is its own music-outreach pipeline. A DJ record is an
independent DJ / DJ brand — it must be valid with NO radio station, station
affiliation is optional metadata only, and a radio-station contact is NEVER
converted into a DJ record (KEXP's music director/DJ inbox stays a radio
station contact).

This file pins that contract backend-side (records exist without any
station; station references are non-enforced metadata), frontend-side (the
station page carries no "Add to DJs" promotion), and schema-side (the djs
table marks station columns nullable with no FK to stations). It also
verifies the operator-created DJ create/list/delete flow still works with a
completely empty station registry, and that outreach uses the existing
system (target_type 'dj', identity_key 'dj:<id>'), never auto-sending.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.routes import dispatch
from backend.webapp import DEFAULT_STATIC_ROOT
from database.service import PersistenceService
from djs import service as dj_service

INDEPENDENT_DJ = {
    "name": "DJ Ashanti",
    "stage_name": "Ashanti",
    "role": "open-format DJ",
    "country": "US",
    "genres": ["hip hop", "dancehall"],
    "source_urls": ["https://djashanti.example/"],
    "channels": [
        {"channel": "email",
         "value": "booking@djashanti.example",
         "source_url": "https://djashanti.example/contact"},
        {"channel": "instagram",
         "value": "https://instagram.com/djashanti",
         "source_url": "https://djashanti.example/contact"},
    ],
}

RADIO_STATION_WITH_DJ_CONTACT = [{
    "name": "W Test FM",
    "website": "https://wtestfm.example",
    "station_type": "community",
    "genres": ["rock"],
    "formats": ["music"],
    "country": "US",
    "city": "Austin",
    "confidence_score": 0.7,
    "status": "enriched",
    "contacts": [
        {"name": None, "role": "dj", "email": "dj@wtestfm.example",
         "contact_uid": "wtest_dj"},
        {"name": "Alex", "role": "music_director",
         "email": "md@wtestfm.example", "contact_uid": "wtest_md"},
    ],
    "submission": None,
}]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.service = PersistenceService(os.path.join(self._tmp, "test.db"))

    def tearDown(self):
        self.service.close()

    def _dj_total(self):
        return self.service.list_djs(limit=1000, offset=0)[1]

    def _outreach_total(self):
        return self.service.list_outreach(limit=1000, offset=0)[1]

    def _post_dj(self, payload):
        return dispatch(self.service, "POST", "/api/v1/djs", {},
                        json.dumps(payload).encode("utf-8"))


class TestDjExistsWithoutStation(_Base):
    """The DJ pipeline must work even when the station DB has ZERO records."""

    def test_create_get_list_delete_with_empty_station_registry(self):
        self.assertEqual(self.service.list_stations(limit=1000)[1], 0)
        status, env = self._post_dj(INDEPENDENT_DJ)
        self.assertEqual(status, 201)
        self.assertTrue(env["ok"])
        detail = env["data"]
        self.assertEqual(detail["name"], "DJ Ashanti")
        # Station fields are optional metadata — absent here, never guessed.
        self.assertIsNone(detail["station_key"])
        self.assertIsNone(detail["station_name"])
        self.assertEqual(self._dj_total(), 1)

        status, env = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(status, 200)
        self.assertEqual(env["data"]["total"], 1)
        self.assertEqual(env["data"]["djs"][0]["name"], "DJ Ashanti")
        self.assertIsNone(env["data"]["djs"][0]["station_key"])

        status, env = dispatch(
            self.service, "DELETE",
            f"/api/v1/djs/{detail['dj_id']}", {})
        self.assertEqual(status, 200)
        self.assertEqual(self._dj_total(), 0)

    def test_dj_station_reference_is_plain_optional_metadata(self):
        # A station key that exists nowhere is acceptable: it is metadata, not
        # a required relation, so the record must not be rejected.
        payload = dict(INDEPENDENT_DJ)
        payload["station_key"] = "domain:no-such-station.example"
        payload["station_name"] = "Nothing Here"
        status, env = self._post_dj(payload)
        self.assertEqual(status, 201)
        self.assertEqual(env["data"]["station_key"],
                         "domain:no-such-station.example")

    def test_unknown_fields_stay_null_nothing_fabricated(self):
        status, env = self._post_dj(INDEPENDENT_DJ)
        detail = env["data"]
        self.assertIsNone(detail["city"])
        self.assertIsNone(detail["state_or_region"])
        self.assertIsNone(detail["platform"])
        self.assertIsNone(detail["program"])
        self.assertEqual(detail["formats"], [])
        # Country is recorded but never verified → honestly "unverified".
        self.assertEqual(detail["location_status"], "unverified")


class TestNoStationContactAutoConversion(_Base):
    """A radio-station contact must never become an independent DJ record."""

    def setUp(self):
        super().setUp()
        self.service.ingest_intelligence(
            [dict(row) for row in RADIO_STATION_WITH_DJ_CONTACT], source="test")
        self.kx = "domain:wtestfm.example"

    def test_station_dj_contact_present_in_station_pipeline_only(self):
        # The contact is stored with the station (radio-station ecosystem)…
        contacts = self.service.get_station_contacts(self.kx)
        self.assertTrue(any(c.get("role") == "dj" for c in contacts),
                        "the role='dj' contact lives on the station")
        # …and it does NOT create any DJ record by itself.
        self.assertEqual(self._dj_total(), 0, "no DJ is auto-created")

    def test_dj_pipeline_fills_only_from_operator_action(self):
        # Explicit operator action is what populates the DJ pipeline.
        self.assertEqual(self._dj_total(), 0)
        status, env = self._post_dj(INDEPENDENT_DJ)
        self.assertEqual(status, 201)
        self.assertEqual(self._dj_total(), 1)

    def test_no_duplicate_dj_from_station_lookup(self):
        # There is no derivation path to duplicate: listing DJs via the
        # station filter finds only operator-created records (none here).
        _, env = dispatch(self.service, "GET", "/api/v1/djs",
                          {"station": [self.kx], "limit": ["200"]})
        self.assertEqual(env["data"]["total"], 0)
        status, env = self._post_dj(INDEPENDENT_DJ)
        self.assertEqual(status, 201)
        _, env = dispatch(self.service, "GET", "/api/v1/djs",
                          {"station": [self.kx], "limit": ["200"]})
        self.assertEqual(env["data"]["total"], 0)

    def test_dj_outreach_is_separate_from_station_outreach(self):
        # Reuses the existing outreach system; DJ records target_type 'dj'.
        _, env = self._post_dj(INDEPENDENT_DJ)
        dj_id = env["data"]["dj_id"]
        _, env = dispatch(self.service, "GET", f"/api/v1/djs/{dj_id}", {})
        self.assertEqual(self._outreach_total(), 0)
        # Creating outreach for this DJ targets the dj identity, never the
        # station, and requires no station involvement.
        status, env = dispatch(self.service, "POST", "/api/v1/outreach", {},
                               json.dumps({
                                   "recipient": {
                                       "contact_uid": "dj:" + dj_id,
                                       "identity_key": "dj:" + dj_id,
                                       "target_type": "dj",
                                       "name": "DJ Ashanti",
                                       "role": "open-format DJ",
                                       "email": "booking@djashanti.example",
                                       "organization": "DJ Ashanti",
                                       "outreach_class": "email",
                                       "source_url":
                                           "https://djashanti.example/contact",
                                   },
                                   "track": None,
                                   "subject": "New release",
                                   "message": "Hi",
                               }).encode("utf-8"))
        self.assertEqual(status, 201)
        self.assertEqual(self._outreach_total(), 1)
        # The station pipeline stays untouched by the DJ record.
        self.assertTrue(any(
            c.get("role") == "dj"
            for c in self.service.get_station_contacts(self.kx)))


class TestSchemaAndFrontendIndependence(unittest.TestCase):
    """Static contracts: no station dependency in schema / station page."""

    def test_djs_schema_station_columns_are_nullable_without_fk(self):
        import re
        schema = (Path(__file__).resolve().parents[1]
                  / "database" / "schema.py").read_text(encoding="utf-8")
        djs_block = schema[schema.index("CREATE TABLE IF NOT EXISTS djs ("):
                           schema.index("CREATE INDEX IF NOT EXISTS idx_djs_station")]
        self.assertTrue(
            re.search(r"station_key\s+TEXT,\s+-- stations\.identity_key",
                      djs_block),
            "station_key is plain optional metadata, not a required FK")
        self.assertTrue(re.search(r"station_name\s+TEXT,", djs_block))
        self.assertNotIn("REFERENCES stations", djs_block,
                         "a DJ must never carry a required station FK")

    def test_station_page_has_no_add_to_djs_promotion(self):
        src = (Path(DEFAULT_STATIC_ROOT) / "js" / "views" / "station.js")
        text = src.read_text(encoding="utf-8")
        for banned in ("Add to DJs", "api.createDj", "isDjRoleContact",
                       "djToDjsControl", "addContactToDjs"):
            self.assertNotIn(banned, text,
                             f"station page must not promote station contacts "
                             f"into the DJ pipeline ({banned!r})")
        self.assertNotIn("djHref", text, "station page has no DJ routing")

    def test_app_css_has_no_add_djs_styles(self):
        css = (Path(DEFAULT_STATIC_ROOT) / "css" / "app.css").read_text(
            encoding="utf-8")
        self.assertNotIn(".add-djs", css)

    def test_dj_views_render_independently_without_station(self):
        profile = (Path(DEFAULT_STATIC_ROOT) / "js" / "views"
                   / "djProfile.js").read_text(encoding="utf-8")
        self.assertIn("detail.station_name || detail.platform || null", profile,
                      "DJ profile treats station as optional context")
        self.assertIn("target_type: \"dj\"", profile,
                      "DJ outreach uses the existing outreach system")
        listing = (Path(DEFAULT_STATIC_ROOT) / "js" / "views"
                   / "djs.js").read_text(encoding="utf-8")
        self.assertIn("djHref", listing)
        self.assertIn("outreach", listing)


if __name__ == "__main__":
    unittest.main()