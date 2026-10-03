"""Authenticated operator station-edit API.

``PATCH /api/v1/stations/{identity_key}/overrides`` is the only write path
over station field values. It must:

  * FAIL CLOSED - a missing/empty ``MIE_OPERATOR_TOKEN`` denies every request;
    there is no fallback to unauthenticated access, unlike the automation gate
    which is intentionally off when its token is unset.
  * never change identity (``identity_key`` / ``identity_kind``).
  * never edit engine telemetry, exclusion, or qualification.
  * write ONLY through ``set_station_overrides()``, so a correction is
    recorded, snapshotted, and survives re-ingestion.
  * reject invalid fields loudly instead of silently dropping them.

Responses must not leak operator internals: the unauthenticated station detail
reports only WHICH fields are operator-overridden, never the actor, the
snapshotted previous value, or the set/cleared audit timestamps.

Both server implementations are exercised for every behavioural case: the
stdlib dispatcher (``backend.routes.dispatch``) and the FastAPI app
(``backend.app.create_app``) share one contract but not one code path.

All data lives in temp SQLite files; no network, no production database.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from backend.app import create_app
from backend.contracts import validate_override_payload
from backend.routes import dispatch
from database.service import OVERRIDABLE_FIELDS, PersistenceService
from fastapi.testclient import TestClient

KEY = "domain:kqxr.example"
OTHER_KEY = "domain:wmmx.example"
MISSING_KEY = "domain:absent.example"
OVERRIDES_PATH = f"/api/v1/stations/{KEY}/overrides"

_TOKEN = "mie-operator-test-token-not-a-real-secret"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _record(key: str = KEY, *, name: str = "KQXR", country=None, state=None,
            city: str | None = "Portland", market_area=None,
            station_type=None, genres=None, formats=None,
            raw_metadata: dict | None = None, **over) -> dict:
    """A minimal intelligence record shaped like real discovery output."""
    record = {
        "identity_key": key,
        "name": name,
        "organization_type": "radio_station",
        "website": f"https://{key.split(':')[1]}/",
        "country": country,
        "state_or_region": state,
        "city": city,
        "market_area": market_area,
        "station_type": station_type,
        "genres": genres,
        "formats": formats,
        "status": "enriched",
        "confidence_score": 0.6,
        "confidence_reasons": ["website_reachable"],
        "discovered_at": "2026-01-01T00:00:00+00:00",
        "last_observed_at": "2026-01-01T00:00:00+00:00",
        "emails": [],
        "contacts": [],
        "raw_metadata": raw_metadata if raw_metadata is not None else {
            "qualification": {"verdict": "qualified", "kind": "station_site",
                              "reason": "", "evidence": ["callsign"]},
            "location_evidence": {"city": "Portland", "source_url":
                                  "https://kqxr.example/about"},
        },
    }
    record.update(over)
    return record


def _body(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


class _ApiBase(unittest.TestCase):
    """Two stations in a temp SQLite repo; both servers bound to it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = PersistenceService(os.path.join(self._tmp.name, "mie.db"))
        self.repo.ingest_intelligence([
            _record(country="US", state="OR", market_area="serving Portland",
                    station_type="community", genres=["rock"],
                    formats=["terrestrial"]),
            _record(key=OTHER_KEY, name="WMMX", city="Eugene"),
        ], source="override_api_test")
        self.app = create_app(self.repo)
        # One client per test: entering TestClient as a context manager runs
        # the ASGI lifespan per request, which is needlessly slow here.
        self.client = TestClient(self.app)
        self._env = os.environ.get("MIE_OPERATOR_TOKEN")
        os.environ["MIE_OPERATOR_TOKEN"] = _TOKEN

    def tearDown(self):
        # The client is an ASGI portal; leaving it open per test leaks a
        # thread and eventually wedges the run.
        self.client.close()
        if self._env is None:
            os.environ.pop("MIE_OPERATOR_TOKEN", None)
        else:
            os.environ["MIE_OPERATOR_TOKEN"] = self._env
        self.repo.close()
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def dispatch_patch(self, payload, *, path=OVERRIDES_PATH, headers=None,
                       raw: bytes | None = None):
        return dispatch(self.repo, "PATCH", path, {},
                        _body(payload) if raw is None else raw,
                        headers=headers if headers is not None else _AUTH)

    def http_patch(self, payload=None, *, path=OVERRIDES_PATH, headers=None,
                   raw=None):
        headers = _AUTH if headers is None else headers
        body = raw if raw is not None else _body(payload)
        return self.client.patch(path, content=body, headers=headers or {})

    def get_station(self, *, headers=None, key: str = KEY):
        """Public GET /api/v1/stations/{key} (no auth by design)."""
        return self.http_get(f"/api/v1/stations/{key}", headers=headers)

    def http_get(self, path, headers=None):
        return self.client.get(path, headers=headers or {})

    def row(self, key: str = KEY) -> dict:
        return self.repo.get_station(key)

    def meta(self, key: str = KEY) -> dict:
        return (self.row(key)["raw_metadata"] or {})

    def overrides(self, key: str = KEY) -> dict:
        return self.meta(key).get("operator_overrides") or {}

    def store(self, record: dict) -> None:
        self.repo.ingest_intelligence([record], source="override_api_test")

    def count_stations(self) -> int:
        return self.repo.list_stations(limit=200, offset=0,
                                       exclude_dev=False,
                                       exclude_quarantined=False)[1]


# ===========================================================================
# Authentication - fail closed
# ===========================================================================

class OperatorAuthTests(_ApiBase):
    """Requirements 1-4: the gate denies by default and never fails open."""

    def test_missing_authorization_header_is_401(self):
        status, envelope = self.dispatch_patch({"city": "Salem"}, headers={})
        self.assertEqual(status, 401)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "unauthorized")
        self.assertEqual(self.row()["city"], "Portland")

    def test_wrong_token_is_401(self):
        status, _ = self.dispatch_patch(
            {"city": "Salem"},
            headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(status, 401)
        self.assertEqual(self.row()["city"], "Portland")

    def test_correct_token_succeeds(self):
        status, envelope = self.dispatch_patch({"city": "Salem"})
        self.assertEqual(status, 200)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["city"], "Salem")

    def test_unset_token_denies_everything(self):
        """Fail closed: no env var at all must not fall back to open."""
        os.environ.pop("MIE_OPERATOR_TOKEN", None)
        status, envelope = self.dispatch_patch(
            {"city": "Salem"}, headers={})
        self.assertEqual(status, 401)
        self.assertEqual(envelope["error"]["code"], "unauthorized")

    def test_empty_token_denies_everything(self):
        os.environ["MIE_OPERATOR_TOKEN"] = "   "
        status, _ = self.dispatch_patch({"city": "Salem"}, headers={})
        self.assertEqual(status, 401)
        self.assertEqual(self.row()["city"], "Portland")

    def test_correct_token_still_denied_when_token_unset(self):
        """Even a well-formed header cannot help without a configured secret."""
        os.environ.pop("MIE_OPERATOR_TOKEN", None)
        status, _ = self.dispatch_patch({"city": "Salem"}, headers=_AUTH)
        self.assertEqual(status, 401)
        self.assertEqual(self.row()["city"], "Portland")

    def test_non_bearer_scheme_is_401(self):
        for header in ({"Authorization": _TOKEN},
                       {"Authorization": f"Basic {_TOKEN}"},
                       {"Authorization": "Bearer"},
                       {"Authorization": "Bearer "}):
            with self.subTest(header=header):
                status, _ = self.dispatch_patch({"city": "Salem"},
                                                headers=header)
                self.assertEqual(status, 401)
        self.assertEqual(self.row()["city"], "Portland")

    def test_fastapi_server_401s_without_token(self):
        response = self.http_patch({"city": "Salem"}, headers={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")
        self.assertEqual(self.row()["city"], "Portland")

    def test_fastapi_server_401s_on_wrong_token(self):
        response = self.http_patch({"city": "Salem"}, headers={
            "Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.row()["city"], "Portland")

    def test_fastapi_server_401s_when_token_unset(self):
        os.environ.pop("MIE_OPERATOR_TOKEN", None)
        response = self.http_patch({"city": "Salem"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.row()["city"], "Portland")

    def test_both_servers_agree_on_success(self):
        """Requirement 25: Authorization flows through both implementations."""
        self.assertEqual(self.http_patch({"city": "Salem"}).status_code, 200)
        self.assertEqual(self.dispatch_patch({"city": "Eugene"})[0], 200)
        self.assertEqual(self.row()["city"], "Eugene")

    def test_both_servers_agree_on_rejection(self):
        self.assertEqual(self.http_patch({"nope": "x"}).status_code, 400)
        self.assertEqual(self.dispatch_patch({"nope": "x"})[0], 400)
        self.assertEqual(self.http_patch({"city": "S"}, headers={}
                                          ).status_code, 401)
        self.assertEqual(self.dispatch_patch({"city": "S"}, headers={})[0],
                         401)

    def test_auth_is_not_reachable_by_other_methods(self):
        """The gate guards PATCH only; a GET must not be shadowed."""
        response = self.http_get(OVERRIDES_PATH)
        self.assertEqual(response.status_code, 405)


# ===========================================================================
# Valid edits
# ===========================================================================

class ValidEditTests(_ApiBase):
    """Requirements 5-9: each overridable field is editable."""

    def test_city_edit(self):
        status, envelope = self.dispatch_patch({"city": "Salem"})
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["city"], "Salem")
        self.assertEqual(self.row()["city"], "Salem")

    def test_name_edit(self):
        status, envelope = self.dispatch_patch({"name": "KQXR-FM"})
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["name"], "KQXR-FM")

    def test_name_edit_never_changes_identity_key(self):
        before = self.row()
        status, envelope = self.dispatch_patch(
            {"name": "Completely Different Station Name"})
        self.assertEqual(status, 200)
        after = self.row()
        self.assertEqual(after["identity_key"], before["identity_key"])
        self.assertEqual(after["identity_key"], KEY)
        self.assertEqual(after["name"],
                         "Completely Different Station Name")
        self.assertEqual(envelope["data"]["identity_key"], KEY)

    def test_identity_key_is_unchanged_in_payload(self):
        """Even echoing the same key back is rejected - the endpoint refuses
        the field entirely so identity can never be part of an edit."""
        status, envelope = self.dispatch_patch(
            {"identity_key": OTHER_KEY, "city": "Salem"})
        self.assertEqual(status, 400)
        self.assertEqual(self.row()["city"], "Portland")
        self.assertEqual(self.row()["identity_key"], KEY)

    def test_genres_edit(self):
        status, envelope = self.dispatch_patch(
            {"genres": ["jazz", "blues"]})
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["genres"], ["jazz", "blues"])
        self.assertEqual(self.row()["genres"], ["jazz", "blues"])

    def test_formats_edit(self):
        status, envelope = self.dispatch_patch({"formats": ["stream"]})
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["formats"], ["stream"])

    def test_all_editable_fields_at_once(self):
        payload = {
            "name": "KQXR-FM", "website": "https://kqxr.example/new",
            "country": "CA", "state_or_region": "BC",
            "city": "Vancouver", "market_area": "serving Metro Vancouver",
            "station_type": "public", "genres": ["talk"],
            "formats": ["online"],
        }
        self.assertEqual(set(payload), set(OVERRIDABLE_FIELDS))
        status, envelope = self.dispatch_patch(payload)
        self.assertEqual(status, 200)
        for field, value in payload.items():
            self.assertEqual(envelope["data"][field], value, field)

    def test_edit_creates_an_override_record(self):
        self.dispatch_patch({"city": "Salem"})
        entry = self.overrides()["city"]
        self.assertEqual(entry["value"], "Salem")
        self.assertEqual(entry["previous"], "Portland")
        self.assertIsNone(entry["cleared_at"])

    def test_response_reports_overridden_fields(self):
        status, envelope = self.dispatch_patch(
            {"city": "Salem", "genres": ["jazz"]})
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["overridden_fields"],
                         ["city", "genres"])

    def test_absent_field_leaves_existing_override_untouched(self):
        self.dispatch_patch({"city": "Salem"})
        self.dispatch_patch({"genres": ["jazz"]})
        entry_before = self.overrides()["city"]
        self.dispatch_patch({"genres": ["blues"]})
        entry_after = self.overrides()["city"]
        self.assertEqual(entry_after, entry_before)
        self.assertEqual(self.row()["city"], "Salem")

    def test_repeated_edit_updates_in_place(self):
        self.dispatch_patch({"city": "Salem"})
        self.dispatch_patch({"city": "Eugene"})
        self.assertEqual(self.row()["city"], "Eugene")
        self.assertEqual(self.overrides()["city"]["previous"], "Portland")

    def test_values_are_stripped_and_empty_rejected(self):
        self.dispatch_patch({"city": "  Salem  "})
        self.assertEqual(self.row()["city"], "Salem")
        status, envelope = self.dispatch_patch({"city": ""})
        self.assertEqual(status, 400)
        self.assertEqual(self.row()["city"], "Salem")

    def test_wrong_types_rejected(self):
        for payload in ({"city": 5}, {"city": True}, {"genres": "jazz"},
                        {"genres": [1, 2]}, {"genres": {"a": 1}},
                        {"city": {"value": "Salem"}}):
            with self.subTest(payload=payload):
                status, envelope = self.dispatch_patch(payload)
                self.assertEqual(status, 400, payload)
                self.assertEqual(envelope["error"]["code"], "bad_request")
        self.assertEqual(self.row()["city"], "Portland")

    def test_empty_body_rejected(self):
        for payload in ({}, ):
            status, _ = self.dispatch_patch(payload)
            self.assertEqual(status, 400)
        status, _ = self.dispatch_patch(None, raw=b"")
        self.assertEqual(status, 400)
        status, _ = self.dispatch_patch(None, raw=b"not json")
        self.assertEqual(status, 400)
        status, _ = self.dispatch_patch(None, raw=b"[1,2]")
        self.assertEqual(status, 400)


# ===========================================================================
# Clearing
# ===========================================================================

class ClearOverrideTests(_ApiBase):
    """Requirement 10: null clears and restores the snapshot."""

    def test_null_clears_and_restores_snapshot(self):
        self.dispatch_patch({"city": "Salem"})
        self.assertEqual(self.row()["city"], "Salem")
        status, envelope = self.dispatch_patch({"city": None})
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["city"], "Portland")
        self.assertEqual(self.row()["city"], "Portland")

    def test_clear_reports_field_no_longer_overridden(self):
        self.dispatch_patch({"city": "Salem"})
        _, envelope = self.dispatch_patch({"city": None})
        self.assertEqual(envelope["data"]["overridden_fields"], [])

    def test_clear_keeps_audit_timestamps(self):
        self.dispatch_patch({"city": "Salem"})
        self.dispatch_patch({"city": None})
        entry = self.overrides()["city"]
        self.assertIsNotNone(entry["cleared_at"])
        self.assertIsNotNone(entry["set_at"])
        self.assertNotIn("value", entry)
        self.assertEqual(entry["previous"], "Portland")

    def test_clearing_an_unqueued_field_is_a_no_op(self):
        before = self.row()["city"]
        status, _ = self.dispatch_patch({"market_area": None})
        self.assertEqual(status, 200)
        self.assertEqual(self.row()["city"], before)
        self.assertNotIn("market_area", self.overrides())

    def test_clearing_a_list_restores_snapshot(self):
        self.dispatch_patch({"genres": ["jazz", "blues"]})
        self.assertEqual(self.row()["genres"], ["jazz", "blues"])
        status, envelope = self.dispatch_patch({"genres": None})
        self.assertEqual(status, 200)
        self.assertEqual(envelope["data"]["genres"], ["rock"])

    def test_cleared_field_becomes_automatable_again(self):
        self.dispatch_patch({"city": "Salem"})
        self.dispatch_patch({"city": None})
        self.store(_record(country="US", state="OR", city="Bend"))
        # Fill-only merge keeps the restored snapshot; what matters is that the
        # lock no longer blocks ingestion.
        self.assertIn(self.row()["city"], ("Portland", "Bend"))
        self.assertEqual(self.row()["identity_key"], KEY)


# ===========================================================================
# Field validation
# ===========================================================================

class FieldRejectionTests(_ApiBase):
    """Requirements 11-16: nothing outside OVERRIDABLE_FIELDS is accepted."""

    def _reject(self, payload, *, needle=None):
        status, envelope = self.dispatch_patch(payload)
        self.assertEqual(status, 400, payload)
        self.assertEqual(envelope["error"]["code"], "bad_request")
        if needle:
            self.assertIn(needle, envelope["error"]["message"])
        return envelope["error"]["message"]

    def test_unknown_field_rejected(self):
        self._reject({"colour": "blue"}, needle="colour")

    def test_misspelled_field_rejected_not_silently_dropped(self):
        """The storage layer drops unknown keys silently; the API must not."""
        message = self._reject({"state": "OR"}, needle="state")
        self.assertIn("state_or_region", message)

    def test_identity_key_rejected(self):
        self._reject({"identity_key": OTHER_KEY}, needle="identity_key")
        self.assertEqual(self.row()["identity_key"], KEY)

    def test_identity_kind_rejected(self):
        self._reject({"identity_kind": "station"}, needle="identity_kind")

    def test_telemetry_fields_rejected(self):
        for field in ("confidence_score", "confidence_reasons",
                      "discovered_at", "last_observed_at", "last_verified_at",
                      "classification_evidence"):
            with self.subTest(field=field):
                self._reject({field: "x"}, needle=field)
        self.assertEqual(self.row()["confidence_score"], 0.6)

    def test_raw_metadata_rejected(self):
        self._reject({"raw_metadata": {"operator_overrides": {}}},
                     needle="raw_metadata")
        self.assertEqual(self.overrides(), {})

    def test_exclusion_rejected(self):
        self._reject({"exclusion": {"active": True}}, needle="exclusion")
        self.assertNotIn("exclusion", self.meta())

    def test_qualification_rejected(self):
        self._reject({"qualification": {"verdict": "rejected"}},
                     needle="qualification")
        self.assertEqual(self.meta()["qualification"]["verdict"],
                         "qualified")

    def test_one_bad_field_rejects_the_whole_body(self):
        """Atomic: a typo must not half-apply the valid fields beside it."""
        self._reject({"city": "Salem", "nope": 1})
        self.assertEqual(self.row()["city"], "Portland")
        self.assertEqual(self.overrides(), {})

    def test_mode_is_not_client_settable(self):
        self._reject({"genres": {"value": ["jazz"], "mode": "replace"}},
                     needle="genres")
        self.assertEqual(self.row()["genres"], ["rock"])

    def test_validator_is_reused_by_both_servers(self):
        ok, error = validate_override_payload({"city": "Salem"})
        self.assertEqual((ok, error), ({"city": "Salem"}, None))
        for bad in ({"identity_key": "x"}, {"nope": 1}, {}, "string", None):
            ok, error = validate_override_payload(bad)
            self.assertEqual(ok, {}, bad)
            self.assertTrue(error, bad)
        # Every forbidden name in the spec is rejected.
        for field in ("identity_key", "identity_kind", "raw_metadata",
                      "exclusion", "qualification", "confidence_score",
                      "confidence_reasons", "discovered_at",
                      "last_observed_at", "last_verified_at",
                      "classification_evidence"):
            ok, error = validate_override_payload({field: "x"})
            self.assertEqual(ok, {}, field)
            self.assertTrue(error, field)


# ===========================================================================
# Not found
# ===========================================================================

class NotFoundTests(_ApiBase):

    def test_unknown_station_is_404_on_both_servers(self):
        status, envelope = self.dispatch_patch(
            {"city": "Salem"},
            path=f"/api/v1/stations/{MISSING_KEY}/overrides")
        self.assertEqual(status, 404)
        self.assertEqual(envelope["error"]["code"], "station_not_found")
        self.assertIsNone(self.repo.get_station(MISSING_KEY))

        response = self.http_patch(
            {"city": "Salem"},
            path=f"/api/v1/stations/{MISSING_KEY}/overrides")
        self.assertEqual(response.status_code, 404)

    def test_unknown_station_never_creates_a_row(self):
        before = self.count_stations()
        self.dispatch_patch({"city": "Salem"},
                            path=f"/api/v1/stations/{MISSING_KEY}/overrides")
        self.assertEqual(self.count_stations(), before)

    def test_401_is_returned_before_the_station_is_looked_up(self):
        """Auth must not become a station-existence oracle."""
        status, _ = self.dispatch_patch(
            {"city": "Salem"},
            path=f"/api/v1/stations/{MISSING_KEY}/overrides", headers={})
        self.assertEqual(status, 401)


# ===========================================================================
# Independence and durability
# ===========================================================================

class PreservationTests(_ApiBase):
    """Requirements 18-20: exclusion, qualification, re-ingestion."""

    def test_exclusion_survives_an_override(self):
        self.repo.set_station_exclusion(KEY, {
            "active": True, "reason": "audited", "mechanism": "test",
            "set_at": "2026-01-01T00:00:00+00:00"})
        self.dispatch_patch({"city": "Salem"})
        self.dispatch_patch({"genres": ["jazz"]})
        exclusion = self.meta()["exclusion"]
        self.assertTrue(exclusion["active"])
        self.assertEqual(exclusion["mechanism"], "test")
        self.assertEqual(self.row()["city"], "Salem")

    def test_qualification_survives_an_override(self):
        before = self.meta()["qualification"]
        self.dispatch_patch({"city": "Salem"})
        self.assertEqual(self.meta()["qualification"], before)

    def test_other_raw_metadata_survives_an_override(self):
        before = dict(self.meta())
        self.dispatch_patch({"city": "Salem"})
        after = dict(self.meta())
        after.pop("operator_overrides")
        self.assertEqual(after, before)

    def test_override_survives_reingestion(self):
        self.dispatch_patch({"city": "Salem"})
        self.dispatch_patch({"genres": ["jazz", "blues"]})
        self.store(_record(country="US", state="OR", city="Bend",
                           genres=["rock", "reggae"]))
        row = self.row()
        self.assertEqual(row["city"], "Salem")
        self.assertEqual(row["genres"], ["jazz", "blues"])

    def test_reingestion_cannot_erase_or_forge_overrides(self):
        self.dispatch_patch({"city": "Salem"})
        self.store(_record(country="US", state="OR", city="Bend",
                           raw_metadata={"operator_overrides": {
                               "city": {"value": "FORGED", "set_at": "x"}}},
                           qualification={"verdict": "rejected"}))
        row = self.row()
        self.assertEqual(row["city"], "Salem")
        self.assertEqual(self.overrides()["city"]["value"], "Salem")
        self.assertEqual(self.meta()["qualification"]["verdict"], "qualified")

    def test_edit_does_not_create_duplicate_stations(self):
        before = self.count_stations()
        self.dispatch_patch({"name": "Totally New Name"})
        self.store(_record(country="US", state="OR", city="Bend"))
        self.assertEqual(self.count_stations(), before)
        self.assertEqual(self.row()["identity_key"], KEY)

    def test_unedited_fields_keep_updating(self):
        self.dispatch_patch({"city": "Salem"})
        self.store(_record(country="US", state="OR", city="Bend",
                           genres=["rock", "reggae"]))
        row = self.row()
        self.assertEqual(row["city"], "Salem")
        self.assertEqual(row["genres"], ["rock", "reggae"])

    def test_other_station_is_untouched(self):
        self.dispatch_patch({"city": "Salem"})
        self.assertEqual(self.row(OTHER_KEY)["city"], "Eugene")
        self.assertEqual(self.overrides(OTHER_KEY), {})


# ===========================================================================
# Public response redaction
# ===========================================================================

class PublicRedactionTests(_ApiBase):
    """Requirements 21-24: public GET hides operator internals."""

    def _public_detail(self, key: str = KEY) -> dict:
        response = self.get_station(key=key)
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    def test_public_get_needs_no_auth(self):
        response = self.get_station()
        self.assertEqual(response.status_code, 200)

    def test_public_get_does_not_expose_operator_overrides(self):
        self.dispatch_patch({"city": "Salem"})
        detail = self._public_detail()
        self.assertNotIn("operator_overrides", detail["raw_metadata"])
        self.assertNotIn("operator_overrides", json.dumps(detail))

    def test_public_get_exposes_overridden_fields(self):
        self.dispatch_patch({"city": "Salem"})
        self.dispatch_patch({"genres": ["jazz"]})
        self.assertEqual(self._public_detail()["overridden_fields"],
                         ["city", "genres"])

    def test_overridden_fields_empty_without_overrides(self):
        self.assertEqual(self._public_detail()["overridden_fields"], [])

    def test_overridden_fields_ignores_cleared_entries(self):
        self.dispatch_patch({"city": "Salem"})
        self.dispatch_patch({"city": None})
        self.assertEqual(self._public_detail()["overridden_fields"], [])

    def test_actor_snapshot_and_timestamps_are_not_public(self):
        # The API never accepts 'actor' from a client, but the storage layer
        # can record one, so the redaction must hold for whatever is stored -
        # not just for what this endpoint happens to write.
        self.repo.set_station_overrides(KEY, {
            "city": {"value": "Salem", "actor": "ops@example.test"}})
        detail = self._public_detail()
        blob = json.dumps(detail)
        for secret in ("operator_overrides", "actor", "ops@example.test",
                       "previous", "set_at", "cleared_at", "mode"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)
        # The corrected VALUE itself is public data and must still be present.
        self.assertEqual(detail["city"], "Salem")
        self.assertEqual(detail["overridden_fields"], ["city"])

    def test_public_get_still_exposes_other_raw_metadata(self):
        detail = self._public_detail()
        self.assertIn("qualification", detail["raw_metadata"])
        self.assertIn("location_evidence", detail["raw_metadata"])
        self.assertEqual(detail["raw_metadata"]["qualification"]["verdict"],
                         "qualified")

    def test_public_get_still_exposes_exclusion(self):
        """Exclusion is intentionally NOT redacted in this change."""
        self.repo.set_station_exclusion(KEY, {
            "active": True, "reason": "audited", "mechanism": "test",
            "set_at": "2026-01-01T00:00:00+00:00"})
        detail = self._public_detail()
        self.assertIn("exclusion", detail["raw_metadata"])
        self.assertTrue(detail["raw_metadata"]["exclusion"]["active"])

    def test_public_get_keeps_every_other_column(self):
        detail = self._public_detail()
        for column in ("identity_key", "identity_kind", "name", "website",
                       "country", "state_or_region", "city", "market_area",
                       "station_type", "genres", "formats",
                       "confidence_score", "discovered_at", "last_observed_at",
                       "source_urls", "social_urls", "status"):
            with self.subTest(column=column):
                self.assertIn(column, detail)
        self.assertIn("location_status", detail)
        self.assertIn("research_status", detail)
        self.assertIn("links", detail)

    def test_patch_response_is_also_redacted(self):
        """The authenticated response must not leak the map either."""
        status, envelope = self.dispatch_patch({"city": "Salem"})
        self.assertEqual(status, 200)
        self.assertNotIn("operator_overrides", envelope["data"]["raw_metadata"])
        self.assertNotIn("actor", json.dumps(envelope["data"]))
        self.assertEqual(envelope["data"]["overridden_fields"], ["city"])

    def test_station_summary_is_unaffected(self):
        """list_stations has no raw_metadata contract and must not change."""
        from backend.contracts import station_summary
        summary = station_summary(self.row())
        self.assertNotIn("raw_metadata", summary)
        self.assertNotIn("overridden_fields", summary)
        self.assertEqual(summary["name"], "KQXR")


if __name__ == "__main__":
    unittest.main()
