"""Operator field overrides survive discovery re-ingestion.

An operator must be able to correct a station's name, website, location,
market, type, genres or formats by hand, and the next discovery/research run
must NOT overwrite that correction — while every field the operator did not
override keeps updating normally.

The lock is stored in the existing ``raw_metadata`` JSON blob (no second
station table, no migration), carried across ingestion verbatim so a re-ingest
can neither erase nor forge it, and cleared reversibly by restoring the
automated value snapshotted when the override was first created.

Engine-owned telemetry (confidence, timestamps, evidence) is deliberately
un-overridable, and the mechanism is INDEPENDENT of station exclusion:
exclusion decides visibility, overrides decide which values win.

All data lives in temp SQLite files; no network, no production database.
"""

from __future__ import annotations

import copy
import os
import tempfile
import unittest

from database.repository import IntelligenceRepository
from database.service import (
    OVERRIDABLE_FIELDS,
    PersistenceService,
    _active_override,
)

KEY = "domain:kqxr.example"
OTHER_KEY = "domain:wmmx.example"


def _record(key: str = KEY, *, name: str = "KQXR", website: str | None = None,
            country: str | None = None, state: str | None = None,
            city: str | None = None, market_area: str | None = None,
            station_type: str | None = None,
            genres: list[str] | None = None,
            formats: list[str] | None = None,
            raw_metadata: dict | None = None,
            **over) -> dict:
    """A minimal intelligence record shaped like real discovery output."""
    record = {
        "identity_key": key,
        "name": name,
        "organization_type": "radio_station",
        "website": website if website is not None else f"https://{key.split(':')[1]}/",
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
        },
    }
    record.update(over)
    return record


class _OverrideBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = PersistenceService(os.path.join(self._tmp.name, "mie.db"))
        self.store(_record(key=KEY, name="KQXR", country="US", state="OR",
                           city="Portland", market_area="serving the Portland area",
                           station_type="community", genres=["rock"],
                           formats=["terrestrial"]))
        self.store(_record(key=OTHER_KEY, name="WMMX"))

    def tearDown(self):
        self.repo.close()
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def store(self, record: dict) -> None:
        self.repo.ingest_intelligence([record], source="override_test")

    def row(self, key: str = KEY) -> dict:
        return self.repo.get_station(key)

    def meta(self, key: str = KEY) -> dict:
        return self.row(key)["raw_metadata"] or {}

    def overrides(self, key: str = KEY) -> dict:
        return self.meta(key).get("operator_overrides") or {}

    def entry(self, field: str, key: str = KEY) -> dict:
        return self.overrides(key)[field]

    def override(self, field: str, value, key: str = KEY, **extra) -> dict:
        payload = dict(extra)
        payload["value"] = value
        self.assertTrue(self.repo.set_station_overrides(key, {field: payload}))
        return self.entry(field, key)


class ScalarOverrideTests(_OverrideBase):
    """Requirement 1: an operator value wins over contradicting automation."""

    def test_scalar_override_survives_reingestion(self):
        self.override("city", "Salem")
        self.store(_record(key=KEY, name="KQXR", country="US", state="OR",
                           city="Bend", station_type="community",
                           genres=["rock"], formats=["terrestrial"]))
        self.assertEqual(self.row()["city"], "Salem")

    def test_every_overridable_scalar_is_locked(self):
        corrected = {
            "name": "KQXR 98.1 Portland",
            "website": "https://kqxr.example/corrected",
            "country": "CA",
            "state_or_region": "BC",
            "city": "Vancouver",
            "market_area": "serving the Lower Mainland",
            "station_type": "online",
        }
        incoming = _record(
            key=KEY, name="WRONG NAME", website="https://kqxr.example/wrong",
            country="ZZ", state="WRONG", city="WRONG",
            market_area="WRONG", station_type="WRONG")
        self.repo.set_station_overrides(KEY, dict(corrected))
        self.store(incoming)
        for field, expected in corrected.items():
            with self.subTest(field=field):
                self.assertEqual(self.row()[field], expected)

    def test_telemetry_fields_cannot_be_overridden(self):
        before = self.row()
        self.repo.set_station_overrides(KEY, {
            "confidence_score": {"value": 0.99},
            "last_observed_at": {"value": "2099-01-01T00:00:00+00:00"},
            "discovered_at": {"value": "2099-01-01T00:00:00+00:00"},
            "confidence_reasons": {"value": ["fabricated"]},
        })
        after = self.row()
        for field in ("confidence_score", "last_observed_at", "discovered_at"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after["confidence_reasons"], ["website_reachable"])
        self.assertEqual(self.overrides(), {})

    def test_unknown_field_is_rejected_not_stored(self):
        self.repo.set_station_overrides(KEY, {"bogus_field": {"value": "x"}})
        self.assertEqual(self.overrides(), {})
        self.assertNotIn("bogus_field", OVERRIDABLE_FIELDS)


class ListOverrideTests(_OverrideBase):
    """Requirements 5 and 6: replace mode must block the list union."""

    def test_genres_replace_blocks_incoming_union(self):
        self.override("genres", ["jazz"])
        # The incoming record still carries "rock", which _merge_list would
        # otherwise re-append after "jazz".
        self.store(_record(key=KEY, name="KQXR", country="US", state="OR",
                           city="Portland", station_type="community",
                           genres=["rock", "reggae"], formats=["terrestrial"]))
        self.assertEqual(self.row()["genres"], ["jazz"])

    def test_formats_replace_blocks_incoming_union(self):
        self.override("formats", ["online"])
        self.store(_record(key=KEY, name="KQXR", country="US", state="OR",
                           city="Portland", station_type="community",
                           genres=["rock"], formats=["terrestrial", "satellite"]))
        self.assertEqual(self.row()["formats"], ["online"])

    def test_list_override_survives_reingestion(self):
        self.override("genres", ["jazz", "blues"], mode="replace")
        self.store(_record(key=KEY, genres=["punk"]))
        self.assertEqual(self.row()["genres"], ["jazz", "blues"])
        self.assertEqual(self.entry("genres")["value"], ["jazz", "blues"])

    def test_non_overridden_lists_still_union(self):
        self.store(_record(key=KEY, genres=["blues"]))
        self.assertEqual(sorted(self.row()["genres"]), ["blues", "rock"])


class UnaffectedFieldsTests(_OverrideBase):
    """Requirement 4: automation keeps updating everything else."""

    def test_non_overridden_fields_continue_to_be_filled(self):
        self.assertIsNone(self.row()["description"])
        self.override("city", "Salem")
        self.store(_record(key=KEY, name="KQXR", country="US", state="OR",
                           city="Bend", station_type="community",
                           genres=["rock"], formats=["terrestrial"],
                           description="A community station."))
        row = self.row()
        self.assertEqual(row["city"], "Salem")          # locked
        self.assertEqual(row["description"], "A community station.")  # filled

    def test_null_only_fill_still_works_for_overridden_field_after_clear(self):
        self.override("city", "Salem")
        self.store(_record(key=KEY, city="Bend"))
        self.assertEqual(self.row()["city"], "Salem")
        self.repo.set_station_overrides(KEY, {"city": None})
        self.assertEqual(self.row()["city"], "Portland")


class NameOverrideTests(_OverrideBase):
    """Requirement 2: the display name is correctable, identity is not."""

    def test_name_override_survives_without_changing_identity_key(self):
        self.override("name", "KQXR 98.1 Portland")
        self.store(_record(key=KEY, name="KQXR Internet Radio",
                           country="US", state="OR", city="Portland",
                           genres=["rock"], formats=["terrestrial"]))
        row = self.row()
        self.assertEqual(row["name"], "KQXR 98.1 Portland")
        self.assertEqual(row["identity_key"], KEY)
        self.assertEqual(row["identity_kind"], "domain")

    def test_name_override_does_not_create_a_second_row(self):
        before = len(self.repo.list_stations()[0])
        self.override("name", "Corrected Display Name")
        self.store(_record(key=KEY, name="Completely Different Name"))
        after = self.repo.list_stations()[0]
        self.assertEqual(len(after), before)
        self.assertEqual(self.row()["identity_key"], KEY)
        self.assertEqual(self.row()["name"], "Corrected Display Name")


class OverrideRecordIntegrityTests(_OverrideBase):
    """Requirements 3, 8, 9 and 10."""

    def test_override_record_survives_reingestion(self):
        self.override("city", "Salem", actor="ops@example",
                      set_at_hint=None)
        entry_before = copy.deepcopy(self.entry("city"))
        self.store(_record(key=KEY, city="Bend"))
        entry_after = self.entry("city")
        self.assertEqual(entry_after["value"], "Salem")
        self.assertEqual(entry_after["previous"], "Portland")
        self.assertIn("set_at", entry_before)
        self.assertEqual(entry_after["set_at"], entry_before["set_at"])

    def test_entry_carries_only_approved_structured_metadata(self):
        self.override("city", "Salem", actor="ops@example")
        entry = self.entry("city")
        self.assertEqual(
            set(entry),
            {"value", "set_at", "cleared_at", "previous", "actor"})
        # No free-form prose: station_detail publishes raw_metadata verbatim.
        for forbidden in ("reason", "note", "notes", "comment", "memo"):
            self.assertNotIn(forbidden, entry)

    def test_ingestion_cannot_forge_an_override(self):
        hostile = {
            "operator_overrides": {
                "city": {"value": "FORGED", "set_at": "2020-01-01T00:00:00+00:00",
                         "cleared_at": None, "previous": None},
            }
        }
        self.store(_record(key=KEY, city="Portland", raw_metadata=hostile))
        self.assertEqual(self.overrides(), {})
        self.assertEqual(self.row()["city"], "Portland")

    def test_ingestion_cannot_erase_an_override(self):
        self.override("city", "Salem")
        # Ingest writes raw_metadata wholesale with none of our keys.
        self.store(_record(key=KEY, city="Bend", raw_metadata={
            "qualification": {"verdict": "qualified", "kind": "station_site",
                              "reason": "", "evidence": ["callsign"]},
            "homepage_title": "Re-observed",
        }))
        self.assertEqual(self.entry("city")["value"], "Salem")
        self.assertEqual(self.row()["city"], "Salem")
        # ...and the incoming homepage_title landed normally.
        self.assertEqual(self.meta()["homepage_title"], "Re-observed")

    def test_exclusion_and_qualification_siblings_survive(self):
        self.repo.set_station_exclusion(OTHER_KEY, {
            "active": True, "mechanism": "station_exclusion_apply",
            "reason": "audit", "bucket": "denied_host",
            "decision_source": "r.json", "applied_at": "2026-02-01T00:00:00+00:00",
        })
        self.repo.set_station_overrides(OTHER_KEY, {"city": "Vancouver"})
        self.store(_record(key=OTHER_KEY, name="WMMX"))
        meta = self.meta(OTHER_KEY)
        self.assertTrue(meta["exclusion"]["active"])
        self.assertEqual(meta["qualification"]["verdict"], "qualified")
        self.assertEqual(meta["operator_overrides"]["city"]["value"], "Vancouver")

    def test_overrides_do_not_affect_listing_visibility(self):
        self.override("city", "Salem")
        visible = {s["identity_key"] for s in self.repo.list_stations()[0]}
        self.assertIn(KEY, visible)
        hidden = {s["identity_key"] for s in self.repo.list_stations(
            exclude_quarantined=True)[0]}
        self.assertIn(KEY, hidden)

    def test_exclusion_stays_independent_of_overrides(self):
        self.override("city", "Salem")
        self.assertNotIn("exclusion", self.meta())
        # An override on one station never marks another one.
        self.store(_record(key=OTHER_KEY, name="WMMX"))
        self.assertEqual(self.overrides(OTHER_KEY), {})


class ClearingTests(_OverrideBase):
    """Requirement 7: clearing restores the snapshotted automated value."""

    def test_clearing_restores_snapshotted_scalar(self):
        self.assertEqual(self.row()["city"], "Portland")
        self.override("city", "Salem")
        self.assertEqual(self.row()["city"], "Salem")
        self.assertEqual(self.entry("city")["previous"], "Portland")
        self.assertTrue(self.repo.set_station_overrides(KEY, {"city": None}))
        self.assertEqual(self.row()["city"], "Portland")

    def test_clearing_restores_snapshotted_list(self):
        self.override("formats", ["online"])
        self.assertEqual(self.row()["formats"], ["online"])
        self.repo.set_station_overrides(KEY, {"formats": None})
        self.assertEqual(self.row()["formats"], ["terrestrial"])

    def test_clearing_keeps_audit_timestamps(self):
        self.override("city", "Salem")
        self.repo.set_station_overrides(KEY, {"city": None})
        entry = self.entry("city")
        self.assertIsNotNone(entry["cleared_at"])
        self.assertIn("set_at", entry)
        self.assertNotIn("value", entry)
        self.assertEqual(entry["previous"], "Portland")

    def test_cleared_field_is_automatable_again(self):
        self.override("city", "Salem")
        self.repo.set_station_overrides(KEY, {"city": None})
        self.assertIsNone(_active_override(self.meta(), "city"))
        self.store(_record(key=KEY, city="Bend"))
        self.assertEqual(self.row()["city"], "Portland")  # fill-only rule

    def test_clearing_all_overrides(self):
        self.override("city", "Salem")
        self.override("country", "CA")
        self.override("genres", ["jazz"])
        self.assertTrue(self.repo.set_station_overrides(KEY, None))
        row = self.row()
        self.assertEqual(row["city"], "Portland")
        self.assertEqual(row["country"], "US")
        self.assertEqual(row["genres"], ["rock"])
        for field in ("city", "country", "genres"):
            self.assertIsNotNone(self.entry(field)["cleared_at"])

    def test_resetting_an_active_override_keeps_original_snapshot(self):
        self.override("city", "Salem")
        self.override("city", "Eugene")
        self.assertEqual(self.row()["city"], "Eugene")
        self.assertEqual(self.entry("city")["previous"], "Portland")
        self.repo.set_station_overrides(KEY, {"city": None})
        self.assertEqual(self.row()["city"], "Portland")


class StorageContractTests(unittest.TestCase):

    def test_unknown_identity_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = PersistenceService(os.path.join(tmp, "mie.db"))
            try:
                self.assertFalse(repo.set_station_overrides(
                    "domain:nope.example", {"city": "X"}))
                self.assertFalse(repo.set_station_overrides(
                    "domain:nope.example", None))
            finally:
                repo.close()

    def test_declared_on_the_repository_protocol(self):
        self.assertTrue(hasattr(IntelligenceRepository, "set_station_overrides"))
        self.assertTrue(hasattr(PersistenceService, "set_station_overrides"))

    def test_postgres_storage_declares_the_same_method(self):
        from database import pg_store
        self.assertTrue(hasattr(pg_store.PostgresStorage,
                                "set_station_overrides"))

    def test_active_override_helper(self):
        meta = {"operator_overrides": {"city": {"value": "Salem"}}}
        self.assertEqual(_active_override(meta, "city")["value"], "Salem")
        self.assertIsNone(_active_override(meta, "country"))
        self.assertIsNone(_active_override(meta, "missing"))
        released = {"operator_overrides": {
            "city": {"value": "Salem", "cleared_at": "2026-01-01T00:00:00+00:00"}}}
        self.assertIsNone(_active_override(released, "city"))
        self.assertIsNone(_active_override({}, "city"))
        self.assertIsNone(_active_override(None, "city"))

    def test_overridable_fields_are_exactly_the_agreed_set(self):
        self.assertEqual(set(OVERRIDABLE_FIELDS), {
            "name", "website", "country", "state_or_region", "city",
            "market_area", "station_type", "genres", "formats"})


if __name__ == "__main__":
    unittest.main()