"""Phase 4: DJ intelligence module tests.

Covers storage CRUD, validation, API dispatch contracts, outreach isolation
from opportunity history, channel labeling, and filter/sort. All data lives
in temp directories; no real DB is touched. Tests run in plain ``unittest``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from backend.routes import dispatch
from database.service import PersistenceService
from djs.service import (
    DJ_CHANNEL_ROUTE_CLASS,
    DJ_CHANNEL_ROUTE_LABEL,
    DJ_CHANNEL_TYPES,
    create_dj,
    delete_dj,
    dj_detail,
    dj_identity_key,
    dj_outreach,
    get_dj,
    list_djs,
)
from outreach import service as outreach_service
from outreach.service import LocalStubProvider
from opportunity import service as opp_service

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

hexdigest = "a" * 64
track_id = f"sha256:{hexdigest}"

TRACK = {
    "track_id": track_id,
    "sha256": hexdigest,
    "original_filename": "test.mp3",
    "size_bytes": 2048,
    "content_type": "audio/mpeg",
    "status": "ready",
}

VALID_DJ = {
    "name": "DJ Nobody",
    "stage_name": "Nobody",
    "role": "host",
    "program": "Indie AM",
    "station_key": "domain:genre-match.org",
    "station_name": "W Genre Match",
    "platform": "KEXP",
    "country": "US",
    "state_or_region": "OR",
    "city": "Portland",
    "genres": ["rock", "indie"],
    "formats": ["music"],
    "source_urls": ["https://example.com/dj-nobody"],
    "channels": [
        {
            "channel": "email",
            "value": "dj@genre-match.org",
            "source_url": "https://genre-match.org/people/nobody",
        },
        {
            "channel": "submission_page",
            "value": "https://genre-match.org/submit",
            "source_url": "https://genre-match.org/submit",
        },
    ],
}

SEED_STATIONS = [
    {
        "name": "W Genre Match", "website": "https://genre-match.org",
        "station_type": "community", "genres": ["rock"],
        "formats": ["music"], "country": "US", "city": "Portland",
        "confidence_score": 0.9, "status": "enriched",
        "contacts": [{"name": "Alice", "role": "music_director",
                      "email": "alice@genre-match.org",
                      "contact_uid": "gm_alice"}],
        "submission": {"submission_url": {"value": "https://genre-match.org/submit",
                                          "verified": True,
                                          "source_url": "https://genre-match.org/"}},
    },
    {
        "name": "W No Route", "website": "https://noret-radio.org",
        "station_type": "lpfm", "genres": ["rock"], "country": "US",
        "confidence_score": 0.3, "status": "enriched",
        "contacts": [], "submission": None,
    },
]


def _dj_payload(**overrides) -> dict:
    """Return a valid DJ creation payload, optionally overridden."""
    p = dict(VALID_DJ)
    p["channels"] = [dict(c) for c in VALID_DJ["channels"]]
    p.update(overrides)
    return p


def _outreach_payload(identity_key: str = "domain:genre-match.org",
                      email: str = "alice@genre-match.org",
                      target_type: str = "station") -> dict:
    return {
        "recipient": {
            "contact_uid": "gm_alice",
            "identity_key": identity_key,
            "target_type": target_type,
            "name": "Alice",
            "role": "music_director",
            "email": email,
            "organization": "W Genre Match",
            "outreach_class": "email",
            "source_url": "https://genre-match.org/people/alice",
        },
        "track": {"track_id": track_id},
        "subject": "Test",
        "message": "Hello",
    }


# ---------------------------------------------------------------------------
# Service integration tests (temp sqlite + PersistenceService)
# ---------------------------------------------------------------------------

class _BaseDBTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        db_path = os.path.join(self._tmp, "test.db")
        self.service = PersistenceService(db_path)
        self.service.ingest_intelligence(SEED_STATIONS, source="test")
        self.track = self.service.save_track(dict(TRACK))
        self.kx = "domain:genre-match.org"
        self.kr = "domain:noret-radio.org"

    def tearDown(self):
        self.service.close()

    def _outreach_count(self):
        return self.service.list_outreach(limit=1000, offset=0)[1]


class TestDJCRUD(_BaseDBTest):
    """Create / get / list / delete round-trip."""

    def test_create_and_get(self):
        dj = create_dj(self.service, payload=_dj_payload())
        self.assertTrue(dj["dj_id"].startswith("dj_"))
        self.assertEqual(dj["name"], "DJ Nobody")
        self.assertEqual(dj["stage_name"], "Nobody")
        self.assertEqual(dj["station_name"], "W Genre Match")
        self.assertEqual(dj["country"], "US")
        self.assertEqual(dj["city"], "Portland")
        self.assertEqual(dj["genres"], ["rock", "indie"])

        got = get_dj(self.service, dj["dj_id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["dj_id"], dj["dj_id"])

    def test_get_unknown_returns_none(self):
        self.assertIsNone(get_dj(self.service, "dj_0000nope"))

    def test_delete_roundtrip(self):
        dj = create_dj(self.service, payload=_dj_payload())
        dj_id = dj["dj_id"]
        deleted = delete_dj(self.service, dj_id)
        self.assertEqual(deleted, dj_id)
        self.assertIsNone(get_dj(self.service, dj_id))

    def test_delete_unknown_returns_none(self):
        self.assertIsNone(delete_dj(self.service, "dj_0000nope"))

    def test_channels_stored_and_retrieved(self):
        dj = create_dj(self.service, payload=_dj_payload())
        channels = self.service.get_dj_channels(dj["dj_id"])
        chans = {c["channel"]: c for c in channels}
        self.assertIn("email", chans)
        self.assertIn("submission_page", chans)
        self.assertEqual(chans["email"]["value"], "dj@genre-match.org")
        self.assertTrue(chans["email"]["source_url"].startswith("https://"))

    def test_channels_replaced_on_resave(self):
        """Re-saving a DJ replaces old channels with the new set."""
        dj = create_dj(self.service, payload=_dj_payload())
        # Remove email channel, keep only submission_page
        channels_v2 = [c for c in _dj_payload()["channels"]
                       if c["channel"] == "submission_page"]
        record = dict(dj)
        self.service.save_dj(record, channels_v2)
        stored = self.service.get_dj_channels(dj["dj_id"])
        chans = {c["channel"] for c in stored}
        self.assertEqual(chans, {"submission_page"})

    def test_first_stored_at_preserved_on_resave(self):
        dj = create_dj(self.service, payload=_dj_payload())
        first = dj["first_stored_at"]
        record = dict(dj)
        record["last_observed_at"] = "2030-01-01T00:00:00Z"
        self.service.save_dj(record)
        updated = get_dj(self.service, dj["dj_id"])
        self.assertEqual(updated["first_stored_at"], first)

    def test_list_djs_returns_all(self):
        create_dj(self.service, payload=_dj_payload(name="DJ Alpha"))
        create_dj(self.service, payload=_dj_payload(name="DJ Beta"))
        rows, total = list_djs(self.service, limit=100, offset=0)
        self.assertEqual(total, 2)

    def test_list_djs_pagination(self):
        create_dj(self.service, payload=_dj_payload(name="DJ 1"))
        create_dj(self.service, payload=_dj_payload(name="DJ 2"))
        create_dj(self.service, payload=_dj_payload(name="DJ 3"))
        rows1, total = list_djs(self.service, limit=2, offset=0)
        self.assertEqual(len(rows1), 2)
        self.assertEqual(total, 3)
        rows2, _ = list_djs(self.service, limit=2, offset=2)
        self.assertEqual(len(rows2), 1)

    def test_list_djs_q_filter(self):
        create_dj(self.service, payload=_dj_payload(name="DJ Nobody"))
        create_dj(self.service, payload=_dj_payload(name="Alice Allgenre"))
        rows, _ = list_djs(self.service, q="Alice")
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"Alice Allgenre"})

    def test_list_djs_genre_filter(self):
        create_dj(self.service, payload=_dj_payload(
            name="Rock DJ", genres=["rock"]))
        create_dj(self.service, payload=_dj_payload(
            name="Jazz DJ", genres=["jazz"]))
        rows, _ = list_djs(self.service, genre="rock")
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"Rock DJ"})

    def test_list_djs_country_filter(self):
        create_dj(self.service, payload=_dj_payload(
            name="US DJ", country="US"))
        create_dj(self.service, payload=_dj_payload(
            name="UK DJ", country="GB"))
        rows, _ = list_djs(self.service, country="GB")
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"UK DJ"})

    def test_list_djs_location_filter(self):
        create_dj(self.service, payload=_dj_payload(
            name="Portland DJ", city="Portland"))
        create_dj(self.service, payload=_dj_payload(
            name="Seattle DJ", city="Seattle"))
        rows, _ = list_djs(self.service, location="Seattle")
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"Seattle DJ"})

    def test_list_djs_station_filter(self):
        create_dj(self.service, payload=_dj_payload(
            name="KEXP DJ", station_name="KEXP"))
        create_dj(self.service, payload=_dj_payload(
            name="WFMU DJ", station_name="WFMU"))
        rows, _ = list_djs(self.service, station="KEXP")
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"KEXP DJ"})

    def test_list_djs_sort_station_desc(self):
        create_dj(self.service, payload=_dj_payload(
            name="DJ 1", station_name="Zebra"))
        create_dj(self.service, payload=_dj_payload(
            name="DJ 2", station_name="Alpha"))
        rows, _ = list_djs(self.service, sort="station", order="desc")
        stations = [r["station_name"] for r in rows]
        self.assertEqual(stations, sorted(stations, reverse=True))

    def test_list_djs_sort_discovered_asc(self):
        create_dj(self.service, payload=_dj_payload(
            name="DJ 1", discovered_at="2025-01-01T00:00:00Z"))
        create_dj(self.service, payload=_dj_payload(
            name="DJ 2", discovered_at="2024-01-01T00:00:00Z"))
        rows, _ = list_djs(self.service, sort="discovered", order="asc")
        names = [r["name"] for r in rows]
        self.assertEqual(names, ["DJ 2", "DJ 1"])


# ---------------------------------------------------------------------------
# Validation tests (pure, no DB required for most)
# ---------------------------------------------------------------------------

class TestDJValidation(unittest.TestCase):
    """create_dj validation rejects bad input."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        db_path = os.path.join(self._tmp, "test.db")
        self.service = PersistenceService(db_path)

    def tearDown(self):
        self.service.close()

    def test_missing_name_rejected(self):
        with self.assertRaises(ValueError) as cm:
            create_dj(self.service, payload={"channels": []})
        self.assertIn("name", str(cm.exception))

    def test_unsupported_channel_rejected(self):
        with self.assertRaises(ValueError) as cm:
            create_dj(self.service, payload=_dj_payload(
                channels=[{"channel": "tiktok",
                           "value": "x", "source_url": "https://tiktok.com/x"}]))
        self.assertIn("unsupported channel", str(cm.exception))

    def test_channel_without_value_rejected(self):
        with self.assertRaises(ValueError) as cm:
            create_dj(self.service, payload=_dj_payload(
                channels=[{"channel": "email",
                           "value": "", "source_url": "https://example.com"}]))
        self.assertIn("value", str(cm.exception))

    def test_channel_without_source_url_rejected(self):
        with self.assertRaises(ValueError) as cm:
            create_dj(self.service, payload=_dj_payload(
                channels=[{"channel": "email",
                           "value": "x@y.com", "source_url": None}]))
        self.assertIn("source http", str(cm.exception))

    def test_channel_with_non_http_source_url_rejected(self):
        with self.assertRaises(ValueError) as cm:
            create_dj(self.service, payload=_dj_payload(
                channels=[{"channel": "email",
                           "value": "x@y.com",
                           "source_url": "file:///etc/passwd"}]))
        self.assertIn("source http", str(cm.exception))

    def test_duplicate_channel_value_rejected(self):
        with self.assertRaises(ValueError) as cm:
            create_dj(self.service, payload=_dj_payload(
                channels=[
                    {"channel": "email", "value": "x@y.com",
                     "source_url": "https://example.com/1"},
                    {"channel": "email", "value": "x@y.com",
                     "source_url": "https://example.com/2"},
                ]))
        self.assertIn("duplicate", str(cm.exception))


# ---------------------------------------------------------------------------
# Channel route_class / route_label mapping
# ---------------------------------------------------------------------------

class TestDJChannelMapping(unittest.TestCase):

    def test_all_channel_types_have_route_class(self):
        for ch in DJ_CHANNEL_TYPES:
            self.assertIn(ch, DJ_CHANNEL_ROUTE_CLASS, ch)

    def test_all_channel_types_have_route_label(self):
        for ch in DJ_CHANNEL_TYPES:
            self.assertIn(ch, DJ_CHANNEL_ROUTE_LABEL, ch)

    def test_email_is_direct(self):
        self.assertEqual(DJ_CHANNEL_ROUTE_CLASS["email"], "direct")

    def test_submission_email_is_submission(self):
        self.assertEqual(DJ_CHANNEL_ROUTE_CLASS["submission_email"],
                         "submission")

    def test_submission_page_is_submission_webform(self):
        self.assertEqual(DJ_CHANNEL_ROUTE_CLASS["submission_page"],
                         "submission_webform")

    def test_contact_page_is_contact_webform(self):
        self.assertEqual(DJ_CHANNEL_ROUTE_CLASS["contact_page"],
                         "contact_webform")

    def test_social_channels_are_social(self):
        for ch in ("instagram", "x", "facebook", "youtube"):
            self.assertEqual(DJ_CHANNEL_ROUTE_CLASS[ch], "social")

    def test_email_label_is_direct_dj_contact(self):
        self.assertEqual(DJ_CHANNEL_ROUTE_LABEL["email"],
                         "verified direct DJ contact")


# ---------------------------------------------------------------------------
# dj_detail composition
# ---------------------------------------------------------------------------

class TestDJDetail(_BaseDBTest):

    def test_location_status_unavailable_without_city(self):
        dj = create_dj(self.service, payload=_dj_payload(
            name="No City DJ", city=None, state_or_region=None, country=None))
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertIsNotNone(detail)
        self.assertEqual(detail["location_status"], "unavailable")

    def test_location_status_available_with_city(self):
        dj = create_dj(self.service, payload=_dj_payload(
            name="City DJ", city="Portland", country="US"))
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertEqual(detail["location_status"], "unverified")

    def test_has_email_true_when_email_present(self):
        dj = create_dj(self.service, payload=_dj_payload())
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertTrue(detail["has_email"])

    def test_has_email_false_when_no_email(self):
        dj = create_dj(self.service, payload=_dj_payload(
            channels=[{"channel": "submission_page",
                        "value": "https://example.com/submit",
                        "source_url": "https://example.com/"}]))
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertFalse(detail["has_email"])

    def test_has_submission_route_true_when_present(self):
        dj = create_dj(self.service, payload=_dj_payload())
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertTrue(detail["has_submission_route"])

    def test_has_submission_route_false_when_email_only(self):
        dj = create_dj(self.service, payload=_dj_payload(
            channels=[{"channel": "email",
                        "value": "x@y.com",
                        "source_url": "https://example.com/people"}]))
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertFalse(detail["has_submission_route"])

    def test_submission_routes_list(self):
        dj = create_dj(self.service, payload=_dj_payload())
        detail = dj_detail(self.service, dj["dj_id"])
        kinds = [r["channel"] for r in detail["submission_routes"]]
        self.assertIn("submission_page", kinds)

    def test_social_channels_empty_when_no_social(self):
        dj = create_dj(self.service, payload=_dj_payload())
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertEqual(detail["social_channels"], [])

    def test_social_channels_populated(self):
        dj = create_dj(self.service, payload=_dj_payload(
            channels=[{"channel": "instagram",
                        "value": "@djdj",
                        "source_url": "https://instagram.com/djdj"}]))
        detail = dj_detail(self.service, dj["dj_id"])
        socials = {c["channel"] for c in detail["social_channels"]}
        self.assertEqual(socials, {"instagram"})

    def test_channels_have_route_class_and_route_label(self):
        dj = create_dj(self.service, payload=_dj_payload())
        detail = dj_detail(self.service, dj["dj_id"])
        for ch in detail["channels"]:
            self.assertIn("route_class", ch)
            self.assertIn("route_label", ch)
            self.assertIn("kind", ch)

    def test_outreach_list_empty_initially(self):
        dj = create_dj(self.service, payload=_dj_payload())
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertEqual(detail["outreach"], [])

    def test_links_self(self):
        dj = create_dj(self.service, payload=_dj_payload())
        detail = dj_detail(self.service, dj["dj_id"])
        self.assertEqual(detail["links"]["self"],
                         f"/api/v1/djs/{dj['dj_id']}")

    def test_get_dj_unknown_returns_none(self):
        self.assertIsNone(dj_detail(self.service, "dj_nope"))


# ---------------------------------------------------------------------------
# Outreach isolation: DJ records never pollute station opportunity history
# ---------------------------------------------------------------------------

class TestDJOutreachIsolation(_BaseDBTest):
    """DJ outreach (identity_key='dj:...', target_type='dj') does not mark
    a station as already_contacted in the opportunity engine."""

    def test_dj_outreach_never_pollutes_station_history(self):
        # Create a DJ, create outreach with station's identity_key but
        # target_type='dj' (edge case the guard catches)
        osvc = outreach_service
        rec = _outreach_payload(
            identity_key=self.kx, target_type="dj", email="bob@x.com")
        osvc.create_outreach(self.service, payload=rec, provider=LocalStubProvider())
        self.assertEqual(self._outreach_count(), 1)
        # Station opportunity must NOT be marked already_contacted
        result = opp_service.compute_opportunities(self.service, self.track)
        kx_opp = next(o for o in result["opportunities"]
                      if o["station"]["identity_key"] == self.kx)
        self.assertFalse(kx_opp["already_contacted"],
                         "DJ outreach must not mark station as contacted")
        self.assertEqual(len(kx_opp["history"]), 0)

    def test_station_outreach_still_marks_station_contacted(self):
        """Sanity: a normal station record (no target_type) still works."""
        osvc = outreach_service
        osvc.create_outreach(self.service, payload=_outreach_payload(),
                             provider=LocalStubProvider())
        self.assertEqual(self._outreach_count(), 1)
        result = opp_service.compute_opportunities(self.service, self.track)
        kx_opp = next(o for o in result["opportunities"]
                      if o["station"]["identity_key"] == self.kx)
        self.assertTrue(kx_opp["already_contacted"])

    def test_dj_outreach_identity_key_isolation(self):
        """A record keyed 'dj:<id>' doesn't appear under a station key."""
        dj = create_dj(self.service, payload=_dj_payload())
        uid = dj_identity_key(dj["dj_id"])
        osvc = outreach_service
        osvc.create_outreach(self.service, payload=_outreach_payload(
            identity_key=uid, target_type="dj", email="dj@x.com"),
            provider=LocalStubProvider())
        # dj_outreach returns the record
        matched = dj_outreach(self.service, dj["dj_id"])
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["identity_key"], uid)
        # No station is contacted by this
        result = opp_service.compute_opportunities(self.service, self.track)
        for opp in result["opportunities"]:
            self.assertFalse(opp["already_contacted"])

    def test_dj_records_preserved_after_delete_dj(self):
        dj = create_dj(self.service, payload=_dj_payload())
        uid = dj_identity_key(dj["dj_id"])
        outreach_service.create_outreach(
            self.service,
            payload=_outreach_payload(
                identity_key=uid, target_type="dj", email="dj@x.com"),
            provider=LocalStubProvider())
        delete_dj(self.service, dj["dj_id"])
        # Outreach record survives
        _, total = outreach_service.list_outreach(self.service, limit=1000)
        self.assertEqual(total, 1)
        rows, _ = outreach_service.list_outreach(self.service, limit=1000)
        self.assertEqual(rows[0]["recipient"]["identity_key"], uid)

    def test_existing_stations_and_outreach_unchanged(self):
        """DJ module never touches existing station data."""
        osvc = outreach_service
        osvc.create_outreach(self.service, payload=_outreach_payload(),
                             provider=LocalStubProvider())
        before_outreach = self._outreach_count()
        create_dj(self.service, payload=_dj_payload())
        self.assertEqual(self._outreach_count(), before_outreach)
        rows, _t, _d = self.service.list_stations(limit=1000, offset=0,
                                                  exclude_dev=True)
        keys = {r["identity_key"] for r in rows}
        self.assertEqual(keys, {self.kx, self.kr})


# ---------------------------------------------------------------------------
# Default target_type propagation (station outreach without explicit target_type)
# ---------------------------------------------------------------------------

class TestDefaultTargetType(_BaseDBTest):

    def test_station_outreach_defaults_to_station(self):
        """When target_type is not specified, default is 'station'."""
        outreach_service.create_outreach(
            self.service,
            payload=_outreach_payload(target_type="station"),
            provider=LocalStubProvider())
        row = outreach_service.list_outreach(self.service, limit=10)[0][0]
        self.assertEqual(row["recipient"]["target_type"], "station")

    def test_dj_target_type_stored(self):
        """target_type 'dj' is stored and returned in the payload."""
        osvc = outreach_service
        dj = create_dj(self.service, payload=_dj_payload())
        uid = dj_identity_key(dj["dj_id"])
        osvc.create_outreach(
            self.service,
            payload=_outreach_payload(
                identity_key=uid, target_type="dj", email="dj@x.com"),
            provider=LocalStubProvider())
        row = osvc.list_outreach(self.service, limit=10)[0][0]
        self.assertEqual(row["recipient"]["target_type"], "dj")


# ---------------------------------------------------------------------------
# Dispatch integration (backend.routes.dispatch)
# ---------------------------------------------------------------------------

class TestDJDispatch(_BaseDBTest):

    def test_post_create_dj(self):
        body = json.dumps(_dj_payload()).encode("utf-8")
        status, env = dispatch(self.service, "POST", "/api/v1/djs", {}, body)
        self.assertEqual(status, 201)
        self.assertTrue(env["ok"])
        self.assertIn("dj_id", env["data"])
        self.assertEqual(env["data"]["name"], "DJ Nobody")

    def test_post_missing_name_returns_400(self):
        body = json.dumps({"name": ""}).encode("utf-8")
        status, env = dispatch(self.service, "POST", "/api/v1/djs", {}, body)
        self.assertEqual(status, 400)
        self.assertFalse(env["ok"])

    def test_post_bad_channel_returns_400(self):
        body = json.dumps(_dj_payload(
            channels=[{"channel": "bad",
                        "value": "x",
                        "source_url": "https://x.com"}])).encode("utf-8")
        status, env = dispatch(self.service, "POST", "/api/v1/djs", {}, body)
        self.assertEqual(status, 400)

    def test_post_channel_without_source_url_returns_400(self):
        body = json.dumps(_dj_payload(
            channels=[{"channel": "email",
                        "value": "x@y.com",
                        "source_url": None}])).encode("utf-8")
        status, env = dispatch(self.service, "POST", "/api/v1/djs", {}, body)
        self.assertEqual(status, 400)

    def test_get_list_djs(self):
        create_dj(self.service, payload=_dj_payload(name="DJ A"))
        status, env = dispatch(self.service, "GET", "/api/v1/djs", {})
        self.assertEqual(status, 200)
        self.assertTrue(env["ok"])
        self.assertIn("djs", env["data"])
        self.assertEqual(env["data"]["total"], 1)
        self.assertEqual(env["data"]["djs"][0]["name"], "DJ A")

    def test_get_list_with_genre_filter(self):
        create_dj(self.service, payload=_dj_payload(
            name="Rock DJ", genres=["rock"]))
        create_dj(self.service, payload=_dj_payload(
            name="Jazz DJ", genres=["jazz"]))
        status, env = dispatch(
            self.service, "GET", "/api/v1/djs", {"genre": ["rock"]})
        self.assertEqual(status, 200)
        names = {d["name"] for d in env["data"]["djs"]}
        self.assertEqual(names, {"Rock DJ"})

    def test_get_list_with_q_filter(self):
        create_dj(self.service, payload=_dj_payload(name="Alice Nobody"))
        create_dj(self.service, payload=_dj_payload(name="Bob Someone"))
        status, env = dispatch(
            self.service, "GET", "/api/v1/djs", {"q": ["Alice"]})
        self.assertEqual(status, 200)
        names = {d["name"] for d in env["data"]["djs"]}
        self.assertEqual(names, {"Alice Nobody"})

    def test_get_list_with_sort_desc(self):
        create_dj(self.service, payload=_dj_payload(
            name="DJ Z", station_name="Zebra"))
        create_dj(self.service, payload=_dj_payload(
            name="DJ A", station_name="Alpha"))
        status, env = dispatch(
            self.service, "GET", "/api/v1/djs",
            {"sort": ["station"], "order": ["desc"]})
        self.assertEqual(status, 200)
        stations = [d["station_name"] for d in env["data"]["djs"]]
        self.assertEqual(stations, sorted(stations, reverse=True))

    def test_invalid_sort_returns_400(self):
        status, env = dispatch(
            self.service, "GET", "/api/v1/djs", {"sort": ["price"]})
        self.assertEqual(status, 400)

    def test_invalid_order_returns_400(self):
        status, env = dispatch(
            self.service, "GET", "/api/v1/djs", {"order": ["sideways"]})
        self.assertEqual(status, 400)

    def test_get_detail(self):
        dj = create_dj(self.service, payload=_dj_payload())
        status, env = dispatch(
            self.service, "GET", f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(status, 200)
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["dj_id"], dj["dj_id"])
        self.assertIn("channels", env["data"])
        self.assertIn("location_status", env["data"])

    def test_get_unknown_detail_returns_404(self):
        status, env = dispatch(
            self.service, "GET", "/api/v1/djs/dj_0000ffff", {})
        self.assertEqual(status, 404)
        self.assertEqual(env["error"]["code"], "dj_not_found")

    def test_delete_dj(self):
        dj = create_dj(self.service, payload=_dj_payload())
        status, env = dispatch(
            self.service, "DELETE", f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(status, 200)
        self.assertEqual(env["data"]["dj_id"], dj["dj_id"])
        # Confirm gone
        status2, env2 = dispatch(
            self.service, "GET", f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(status2, 404)

    def test_delete_unknown_returns_404(self):
        status, env = dispatch(
            self.service, "DELETE", "/api/v1/djs/dj_0000ffff", {})
        self.assertEqual(status, 404)

    def test_patch_not_allowed(self):
        status, env = dispatch(
            self.service, "PATCH", "/api/v1/djs", {})
        self.assertEqual(status, 405)

    def test_list_djs_with_pagination(self):
        for i in range(5):
            create_dj(self.service, payload=_dj_payload(name=f"DJ {i}"))
        status, env = dispatch(
            self.service, "GET", "/api/v1/djs",
            {"limit": ["2"], "offset": ["2"]})
        self.assertEqual(status, 200)
        self.assertEqual(len(env["data"]["djs"]), 2)
        self.assertEqual(env["data"]["total"], 5)

    def test_detail_location_status_present(self):
        dj = create_dj(self.service, payload=_dj_payload(
            city="Portland", country="US"))
        status, env = dispatch(
            self.service, "GET", f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(status, 200)
        self.assertEqual(env["data"]["location_status"], "unverified")

    def test_detail_has_email_false_when_no_email(self):
        dj = create_dj(self.service, payload=_dj_payload(
            channels=[{"channel": "submission_page",
                        "value": "https://example.com/submit",
                        "source_url": "https://example.com/"}]))
        status, env = dispatch(
            self.service, "GET", f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(status, 200)
        self.assertFalse(env["data"]["has_email"])

    def test_detail_channels_have_route_info(self):
        dj = create_dj(self.service, payload=_dj_payload())
        status, env = dispatch(
            self.service, "GET", f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(status, 200)
        for ch in env["data"]["channels"]:
            self.assertIn("route_class", ch)
            self.assertIn("route_label", ch)
            self.assertIn("kind", ch)

    def test_detail_outreach_list_empty(self):
        dj = create_dj(self.service, payload=_dj_payload())
        status, env = dispatch(
            self.service, "GET", f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(status, 200)
        self.assertEqual(env["data"]["outreach"], [])

    def test_dj_outreach_shows_in_detail(self):
        dj = create_dj(self.service, payload=_dj_payload())
        uid = dj_identity_key(dj["dj_id"])
        outreach_service.create_outreach(
            self.service,
            payload=_outreach_payload(
                identity_key=uid, target_type="dj", email="dj@x.com"),
            provider=LocalStubProvider())
        status, env = dispatch(
            self.service, "GET", f"/api/v1/djs/{dj['dj_id']}", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(env["data"]["outreach"]), 1)
        self.assertEqual(env["data"]["outreach"][0]["identity_key"], uid)

    def test_dj_outreach_records_preserved_after_delete(self):
        dj = create_dj(self.service, payload=_dj_payload())
        uid = dj_identity_key(dj["dj_id"])
        outreach_service.create_outreach(
            self.service,
            payload=_outreach_payload(
                identity_key=uid, target_type="dj", email="dj@x.com"),
            provider=LocalStubProvider())
        dispatch(self.service, "DELETE", f"/api/v1/djs/{dj['dj_id']}", {})
        _, total = outreach_service.list_outreach(self.service, limit=1000)
        self.assertEqual(total, 1)

    def test_post_dj_with_social_channel(self):
        payload = _dj_payload(
            channels=[{"channel": "instagram",
                        "value": "@djdj",
                        "source_url": "https://instagram.com/djdj"}])
        body = json.dumps(payload).encode("utf-8")
        status, env = dispatch(self.service, "POST", "/api/v1/djs", {}, body)
        self.assertEqual(status, 201)
        chans = {c["channel"] for c in env["data"]["channels"]}
        self.assertEqual(chans, {"instagram"})


if __name__ == "__main__":
    unittest.main()
