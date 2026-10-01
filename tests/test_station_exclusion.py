"""Reversible, operator-audited station exclusion.

An audit of the requalification report identified 46 stored records that are
deterministically NOT radio stations. Excluding them must hide them from the
normal listing and from outreach/opportunity selection while changing
nothing else: no record is deleted, no contact/submission/fetch/outreach row
is touched, the live qualification verdict is preserved verbatim, and the
decision is fully reversible.

The 150 records the live run could never reach are structurally ineligible —
a network failure is never a rejection — and the 110 records that need human
review are never written at all.

All data lives in temp SQLite files; no network, no production database.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from backend.routes import dispatch
from database.service import PersistenceService

from discovery.radio.readiness import (
    READINESS_OUTREACH_READY,
    READINESS_REJECTED,
    REASON_OPERATOR_EXCLUDED,
    compute_outreach_readiness,
)

from opportunity.service import compute_opportunities

_SCRIPT_PATH = (Path(__file__).resolve().parents[1] / "scripts"
                / "station_exclusion_apply.py")
_spec = importlib.util.spec_from_file_location(
    "station_exclusion_apply", _SCRIPT_PATH)
excl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(excl)

AUTH = {"Authorization": "Bearer MIE-TEST-TOKEN"}

# Real-looking TLDs on purpose: a reserved-TLD host would be hidden by the
# dev-fixture filter and make these tests pass for the wrong reason.
EXCLUDED_KEY = "domain:news-gov.net"
KEPT_KEY = "domain:kqxr-radio.org"
REVIEW_KEY = "domain:radio-mentions.com"
UNREACHED_KEY = "domain:unreachable-site.org"

TRACK = {"track_id": "trk_exclusion", "title": "Test Track",
         "artist": "Test Artist"}


def _query(query: str) -> dict:
    params: dict[str, list[str]] = {}
    for part in (query or "").lstrip("?").split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        params.setdefault(key, []).append(value)
    return params


def _record(*, key: str, name: str, verdict: str = "qualified",
            **over) -> dict:
    """One stored station with the contacts/submission/fetches to protect."""
    domain = key.split(":", 1)[1]
    record = {
        "identity_key": key,
        "name": name,
        "organization_type": "radio_station",
        "website": f"https://{domain}/",
        "status": "enriched",
        "genres": ["rock"],
        "station_type": "community",
        "country": "US",
        "city": "Portland",
        "confidence_score": 0.9,
        "contacts": [{
            "name": "Dana Marsh",
            "role": "music_director",
            "email": f"music@{domain}",
            "source_url": f"https://{domain}/staff",
            "source_type": "official_website_page",
            "method": "text_rule",
            "discovered_at": "2026-01-01T00:00:00+00:00",
        }],
        "submission": {
            "submission_email": f"music@{domain}",
            "submission_url": {
                "value": f"https://{domain}/submit",
                "source_url": f"https://{domain}/",
                "source_type": "official_website_page",
                "method": "link_rule",
                "discovered_at": "2026-01-01T00:00:00+00:00",
                "also_seen_at": [],
            },
        },
        "fetches": [
            {"url": f"https://{domain}/", "ok": True, "status": 200,
             "error_kind": None, "fetched_at": "2026-01-01T00:00:00+00:00"},
        ],
        "raw_metadata": {
            "qualification": {
                "verdict": verdict,
                "kind": "station_site",
                "reason": "",
                "evidence": ["callsign", "station_type"],
            },
        },
    }
    record.update(over)
    return record


class _StationExclusionBase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "mie.db")
        self.repo = PersistenceService(self.db_path)
        self._store(_record(key=EXCLUDED_KEY, name="Denied Host Station"))
        self._store(_record(key=KEPT_KEY, name="KQXR Radio"))
        self._store(_record(key=REVIEW_KEY, name="Station Mentions",
                            verdict="needs_review"))
        self._store(_record(key=UNREACHED_KEY, name="Unreachable Site",
                            verdict="needs_review"))

    def tearDown(self):
        self.repo.close()
        self._tmp.cleanup()

    def _store(self, record: dict) -> None:
        self.repo.ingest_intelligence([record],
                                      source="station_exclusion_test")

    def _exclude(self, key: str = EXCLUDED_KEY) -> dict:
        target = {
            "active": True,
            "mechanism": excl.MECHANISM,
            "reason": "audit: deterministic denied-host verdict",
            "bucket": "denied_host",
            "decision_source": "requalify-full-v2.json",
            "applied_at": "2026-02-01T00:00:00+00:00",
        }
        self.assertTrue(self.repo.set_station_exclusion(key, target))
        return target

    def _list(self, query: str = "") -> dict:
        status, body = dispatch(
            self.repo, "GET", "/api/v1/stations", _query(query), None,
            headers=AUTH)
        self.assertEqual(status, 200)
        return body["data"]

    def _raw_metadata(self, key: str) -> dict:
        """Stored raw_metadata, read through the service's own connection.

        Reading via a second connection would fight the service's write lock,
        so the fixture deliberately goes through the repository instead.
        """
        return self.repo.get_station(key)["raw_metadata"]

    def _set_raw_metadata(self, key: str, raw) -> None:
        """Write the raw JSON column directly, to test the JSON path."""
        with self.repo._lock, self.repo._conn:
            self.repo._conn.execute(
                "UPDATE stations SET raw_metadata=? WHERE identity_key=?",
                (raw, key))

    def _stored_keys(self) -> set[str]:
        rows, _ = self.repo.list_stations(limit=1000, offset=0)
        return {row["identity_key"] for row in rows}


# ---------------------------------------------------------------------------
# Read-path exclusion
# ---------------------------------------------------------------------------

class ExclusionHidesFromListingTests(_StationExclusionBase):
    """An excluded station vanishes from the normal listing, and only that."""

    def test_excluded_station_is_absent_from_listing(self):
        self._exclude()
        keys = {row["identity_key"] for row in self._list()["stations"]}
        self.assertNotIn(EXCLUDED_KEY, keys)
        self.assertIn(KEPT_KEY, keys)

    def test_status_filter_cannot_re_admit_an_excluded_station(self):
        self._exclude()
        keys = {row["identity_key"]
                for row in self._list("?status=enriched")["stations"]}
        self.assertNotIn(EXCLUDED_KEY, keys)
        self.assertIn(KEPT_KEY, keys)

    def test_hiding_an_excluded_station_changes_no_other_row(self):
        self._exclude()
        data = self._list("?status=enriched")
        names = {row["name"] for row in data["stations"]}
        self.assertIn("KQXR Radio", names)
        # needs_review rows were already hidden; the exclusion adds nothing.
        self.assertNotIn("Station Mentions", names)
        self.assertNotIn("Unreachable Site", names)

    def test_exclusion_is_counted_with_the_other_hidden_rows(self):
        self._exclude()
        data = self._list("?status=enriched")
        self.assertEqual(data["quarantined_excluded"], 3)
        self.assertEqual(data["total"], 1)

    def test_clearing_the_exclusion_restores_the_listing(self):
        self._exclude()
        self.assertNotIn(
            EXCLUDED_KEY,
            {row["identity_key"] for row in self._list()["stations"]})
        self.assertTrue(self.repo.set_station_exclusion(EXCLUDED_KEY, None))
        self.assertIn(
            EXCLUDED_KEY,
            {row["identity_key"] for row in self._list()["stations"]})

    def test_an_inactive_marker_never_hides_a_station(self):
        # A reversal that records ``active: false`` keeps its provenance
        # without hiding anything.
        self.repo.set_station_exclusion(
            EXCLUDED_KEY, {"active": False, "mechanism": excl.MECHANISM,
                           "reversed_at": "2026-02-02T00:00:00+00:00"})
        keys = {row["identity_key"] for row in self._list()["stations"]}
        self.assertIn(EXCLUDED_KEY, keys)


# ---------------------------------------------------------------------------
# Nothing is destroyed
# ---------------------------------------------------------------------------

class ExclusionPreservesEverythingTests(_StationExclusionBase):
    """Exclusion flags a row; it never deletes or rewrites one."""

    def test_station_row_remains_fully_recoverable(self):
        before = copy.deepcopy(self.repo.get_station(EXCLUDED_KEY))
        self._exclude()
        after = self.repo.get_station(EXCLUDED_KEY)
        self.assertIsNotNone(after)
        self.assertEqual(after["name"], before["name"])
        self.assertEqual(after["website"], before["website"])
        self.assertEqual(after["identity_key"], before["identity_key"])
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["genres"], before["genres"])

    def test_no_station_row_is_ever_deleted(self):
        keys_before = self._stored_keys()
        self._exclude()
        self.assertEqual(self._stored_keys(), keys_before)

    def test_contacts_submission_and_fetches_survive(self):
        self._exclude()
        contacts = self.repo.get_station_contacts(EXCLUDED_KEY)
        self.assertTrue(contacts)
        self.assertEqual(contacts[0]["name"], "Dana Marsh")
        submission = self.repo.get_submission(EXCLUDED_KEY)
        self.assertIsNotNone(submission)
        self.assertEqual(submission["submission_email"],
                         "music@news-gov.net")
        fetches = self.repo.get_fetches(EXCLUDED_KEY)
        self.assertEqual(len(fetches), 1)
        self.assertTrue(fetches[0]["ok"])

    def test_existing_outreach_relationship_survives(self):
        self.repo.save_outreach({
            "outreach_id": "om_existing", "identity_key": EXCLUDED_KEY,
            "target_type": "station", "recipient_name": "Dana Marsh",
            "email": "music@news-gov.net", "outreach_class": "email",
            "subject": "Hi", "message": "Hello",
            "status": "ready", "provider": "local_stub",
            "created_at": "2026-01-02T00:00:00+00:00",
            "updated_at": "2026-01-02T00:00:00+00:00",
        })
        self._exclude()
        rows, _total = self.repo.list_outreach(limit=100, offset=0)
        self.assertIn("om_existing", {r["outreach_id"] for r in rows})

    def test_qualification_verdict_survives_byte_identical(self):
        before = self._raw_metadata(EXCLUDED_KEY)["qualification"]
        self._exclude()
        after = self._raw_metadata(EXCLUDED_KEY)
        self.assertEqual(after["qualification"], before)
        self.assertEqual(after["qualification"]["verdict"], "qualified")

    def test_exclusion_keeps_the_reason_and_audit_source(self):
        target = self._exclude()
        marker = self._raw_metadata(EXCLUDED_KEY)["exclusion"]
        self.assertTrue(marker["active"])
        self.assertEqual(marker["reason"], target["reason"])
        self.assertEqual(marker["decision_source"], "requalify-full-v2.json")
        self.assertEqual(marker["bucket"], "denied_host")
        self.assertEqual(marker["mechanism"], excl.MECHANISM)
        self.assertEqual(marker["applied_at"], target["applied_at"])

    def test_exclusion_is_reversible_to_the_exact_prior_metadata(self):
        before = self._raw_metadata(EXCLUDED_KEY)
        self._exclude()
        self.assertTrue(self.repo.set_station_exclusion(EXCLUDED_KEY, None))
        self.assertEqual(self._raw_metadata(EXCLUDED_KEY), before)


# ---------------------------------------------------------------------------
# Readiness / opportunity selection
# ---------------------------------------------------------------------------

def _ready_record(key: str = EXCLUDED_KEY) -> dict:
    """A station that WOULD be outreach_ready if it were not excluded."""
    domain = key.split(":", 1)[1]
    return {
        "identity_key": key,
        "name": "KQXR Radio",
        "website": f"https://{domain}/",
        "submission": {"submission_email": f"music@{domain}"},
        "contacts": [{"name": "Dana Marsh", "role": "music_director",
                      "email": f"music@{domain}"}],
        "raw_metadata": {
            "qualification": {"verdict": "qualified", "kind": "station_site",
                              "reason": "", "evidence": ["callsign"]},
            "contact_channels": {
                "contact_url": {"value": f"https://{domain}/staff",
                                "source_url": f"https://{domain}/"},
            },
        },
    }


class ExclusionBlocksReadinessTests(unittest.TestCase):

    def test_a_genuinely_ready_station_is_ready(self):
        self.assertEqual(
            compute_outreach_readiness(_ready_record())["status"],
            READINESS_OUTREACH_READY)

    def test_exclusion_rejects_a_otherwise_ready_station(self):
        record = _ready_record()
        record["raw_metadata"]["exclusion"] = {
            "active": True, "mechanism": excl.MECHANISM,
            "reason": "audited"}
        out = compute_outreach_readiness(record)
        self.assertEqual(out["status"], READINESS_REJECTED)
        self.assertEqual(out["reasons"], [REASON_OPERATOR_EXCLUDED])
        self.assertIsNone(out["route"])

    def test_exclusion_outranks_a_qualified_verdict(self):
        record = _ready_record()
        record["raw_metadata"]["qualification"]["verdict"] = "qualified"
        record["raw_metadata"]["exclusion"] = {"active": True}
        self.assertEqual(compute_outreach_readiness(record)["status"],
                         READINESS_REJECTED)

    def test_exclusion_evidence_is_reported_for_audit(self):
        record = _ready_record()
        record["raw_metadata"]["exclusion"] = {
            "active": True, "bucket": "denied_host", "reason": "audited"}
        out = compute_outreach_readiness(record)
        self.assertEqual(out["evidence"]["exclusion"]["bucket"],
                         "denied_host")
        self.assertEqual(out["evidence"]["qualification"]["verdict"],
                         "qualified")

    def test_an_inactive_marker_leaves_the_station_ready(self):
        record = _ready_record()
        record["raw_metadata"]["exclusion"] = {"active": False}
        self.assertEqual(compute_outreach_readiness(record)["status"],
                         READINESS_OUTREACH_READY)

    def test_a_relevance_only_marker_does_not_exclude(self):
        # Only the boolean ``active`` flag decides exclusion; provenance
        # fields are never interpreted as a decision.
        record = _ready_record()
        record["raw_metadata"]["exclusion"] = {"reason": "audited"}
        self.assertEqual(compute_outreach_readiness(record)["status"],
                         READINESS_OUTREACH_READY)


class ExclusionBlocksOpportunityTests(_StationExclusionBase):

    def test_excluded_station_is_absent_from_opportunities(self):
        self._exclude()
        result = compute_opportunities(self.repo, TRACK, limit=50)
        keys = {o["station"]["identity_key"] for o in result["opportunities"]}
        self.assertNotIn(EXCLUDED_KEY, keys)
        self.assertIn(KEPT_KEY, keys)

    def test_reversal_restores_the_opportunity(self):
        self._exclude()
        self.assertTrue(self.repo.set_station_exclusion(EXCLUDED_KEY, None))
        result = compute_opportunities(self.repo, TRACK, limit=50)
        keys = {o["station"]["identity_key"] for o in result["opportunities"]}
        self.assertIn(EXCLUDED_KEY, keys)


# ---------------------------------------------------------------------------
# Ingestion must not erase the marker
# ---------------------------------------------------------------------------

class ReingestPreservesExclusionTests(_StationExclusionBase):

    def test_re_ingest_keeps_the_exclusion_marker(self):
        self._exclude()
        self._store(_record(key=EXCLUDED_KEY, name="Denied Host Station"))
        marker = self._raw_metadata(EXCLUDED_KEY)["exclusion"]
        self.assertTrue(marker["active"])
        self.assertEqual(marker["mechanism"], excl.MECHANISM)

    def test_re_ingest_keeps_the_station_hidden(self):
        self._exclude()
        self._store(_record(key=EXCLUDED_KEY, name="Denied Host Station"))
        keys = {row["identity_key"] for row in self._list()["stations"]}
        self.assertNotIn(EXCLUDED_KEY, keys)

    def test_requalification_writing_a_new_verdict_keeps_the_exclusion(self):
        # The whole reason the marker is separate from ``qualification``.
        self._exclude()
        self.repo.update_station_qualification(EXCLUDED_KEY, {
            "verdict": "qualified", "kind": "station_site",
            "reason": "", "evidence": ["callsign"]})
        meta = self._raw_metadata(EXCLUDED_KEY)
        self.assertEqual(meta["qualification"]["verdict"], "qualified")
        self.assertTrue(meta["exclusion"]["active"])
        keys = {row["identity_key"] for row in self._list()["stations"]}
        self.assertNotIn(EXCLUDED_KEY, keys)

    def test_setting_a_qualification_never_touches_the_exclusion(self):
        self._exclude()
        before = self._raw_metadata(EXCLUDED_KEY)["exclusion"]
        self.repo.update_station_qualification(EXCLUDED_KEY, None)
        self.assertEqual(self._raw_metadata(EXCLUDED_KEY)["exclusion"],
                         before)

    def test_ingestion_cannot_erase_the_exclusion_marker(self):
        # ``raw_metadata`` is ingested verbatim, so without an operator-owned
        # guard a re-observation carrying an ``exclusion`` key of its own
        # would silently undo the audited decision.
        self._exclude()
        before = self._raw_metadata(EXCLUDED_KEY)["exclusion"]
        self._store(_record(
            key=EXCLUDED_KEY, name="Denied Host Station",
            raw_metadata={"qualification": {"verdict": "needs_review"},
                          "exclusion": None}))
        after = self._raw_metadata(EXCLUDED_KEY)["exclusion"]
        self.assertEqual(after, before)
        self.assertTrue(after["active"])
        self.assertNotIn(
            EXCLUDED_KEY,
            {row["identity_key"] for row in self._list()["stations"]})

    def test_ingestion_cannot_overwrite_the_exclusion_marker(self):
        self._exclude()
        before = self._raw_metadata(EXCLUDED_KEY)["exclusion"]
        self._store(_record(
            key=EXCLUDED_KEY, name="Denied Host Station",
            raw_metadata={"exclusion": {"active": False,
                                        "mechanism": "not_ours"}}))
        self.assertEqual(self._raw_metadata(EXCLUDED_KEY)["exclusion"],
                         before)

    def test_ingestion_cannot_forge_an_exclusion(self):
        # A station with no marker must not be excludable by ingestion alone.
        self.assertNotIn("exclusion", self._raw_metadata(KEPT_KEY))
        self._store(_record(
            key=KEPT_KEY, name="KQXR Radio",
            raw_metadata={"qualification": {"verdict": "qualified"},
                          "exclusion": {"active": True,
                                        "mechanism": "spoofed"}}))
        self.assertNotIn("exclusion", self._raw_metadata(KEPT_KEY))
        self.assertIn(
            KEPT_KEY,
            {row["identity_key"] for row in self._list()["stations"]})


# ---------------------------------------------------------------------------
# raw_metadata JSON regression
# ---------------------------------------------------------------------------

class RawMetadataJsonRegressionTests(_StationExclusionBase):
    """The filter must not corrupt or misparse the JSON column."""

    def test_stations_without_an_exclusion_key_are_unaffected(self):
        meta = self._raw_metadata(KEPT_KEY)
        self.assertNotIn("exclusion", meta)
        keys = {row["identity_key"] for row in self._list()["stations"]}
        self.assertIn(KEPT_KEY, keys)

    def test_null_raw_metadata_is_never_hidden(self):
        # An absent/empty JSON column must not hide a row.
        self._set_raw_metadata(KEPT_KEY, None)
        keys = {row["identity_key"] for row in self._list()["stations"]}
        self.assertIn(KEPT_KEY, keys)

    def test_exclusion_under_an_unrelated_key_is_not_read(self):
        # Only ``$.exclusion.active`` decides exclusion; a same-named key at
        # another depth must not hide a row.
        self._set_raw_metadata(
            KEPT_KEY,
            json.dumps({"audit": {"exclusion": {"active": True}}}))
        keys = {row["identity_key"] for row in self._list()["stations"]}
        self.assertIn(KEPT_KEY, keys)

    def test_exclusion_survives_other_raw_metadata_keys(self):
        self._store(_record(
            key=EXCLUDED_KEY, name="Denied Host Station",
            raw_metadata={
                "qualification": {"verdict": "qualified", "reason": ""},
                "contact_channels": {"contact_url": {
                    "value": "https://news-gov.net/staff",
                    "source_url": "https://news-gov.net/"}},
            }))
        self._exclude()
        meta = self._raw_metadata(EXCLUDED_KEY)
        self.assertTrue(meta["exclusion"]["active"])
        self.assertIn("contact_channels", meta)
        self.assertIn("qualification", meta)

    def test_string_active_value_is_not_treated_as_active(self):
        # SQLite json_extract returns the JSON type; only a real boolean
        # counts as active, so a stray string can never hide a row.
        self._set_raw_metadata(
            KEPT_KEY, json.dumps({"exclusion": {"active": "true"}}))
        keys = {row["identity_key"] for row in self._list()["stations"]}
        self.assertIn(KEPT_KEY, keys)


# ---------------------------------------------------------------------------
# The apply script
# ---------------------------------------------------------------------------

def _report(*records: dict) -> dict:
    return {"generated_at": "2026-09-30T18:20:34+00:00",
            "records_total": len(records), "records": list(records)}


def _audited(domain: str, **over) -> dict:
    record = {
        "station_id": f"domain:{domain}",
        "domain": domain,
        "name": f"{domain} site",
        "proposed_verdict": "rejected",
        "proposed_kind": "denied_host",
        "reason": f"host '{domain}' is not a station",
        "determinism": "network_verified",
        "is_transient_network_failure": False,
        "evidence": [f"host={domain}"],
    }
    record.update(over)
    return record


class _ScriptBase(unittest.TestCase):
    """A repository holding one audited, non-stored-safe-domain fixture."""

    AUDITED_DOMAIN = "unlisted-audited.org"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "mie.db")
        self.repo = PersistenceService(self.db_path)
        self.repo.ingest_intelligence(
            [_record(key=f"domain:{self.AUDITED_DOMAIN}",
                     name="Audited Site")],
            source="station_exclusion_script_test")
        self.report = _report(_audited(self.AUDITED_DOMAIN))

    def tearDown(self):
        self.repo.close()
        self._tmp.cleanup()

    def _patched_allowlist(self, domains):
        """Temporarily narrow AUDITED_DOMAINS to *domains*."""
        original = excl.AUDITED_DOMAINS
        excl.AUDITED_DOMAINS = frozenset(domains)
        self.addCleanup(setattr, excl, "AUDITED_DOMAINS", original)

    def _meta(self, key: str) -> dict:
        return self.repo.get_station(key)["raw_metadata"]

    def _run_main(self, argv: list[str]) -> int:
        """Run the script against the temp DB.

        The script opens its own repository, so this fixture's connection is
        closed first: two live SQLite connections to the same file would
        otherwise contend for the write lock.
        """
        self.repo.close()
        try:
            return excl.main(argv)
        finally:
            self.repo = PersistenceService(self.db_path)


class ApplyScriptTests(_ScriptBase):

    def test_plan_resolves_exactly_the_allowlist(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        changes = excl.plan_exclusions(self.repo, self.report)
        self.assertEqual([c["action"] for c in changes], ["exclude"])
        self.assertEqual(changes[0]["station_id"],
                         f"domain:{self.AUDITED_DOMAIN}")

    def test_plan_makes_zero_writes(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        before = self._meta(f"domain:{self.AUDITED_DOMAIN}")
        excl.plan_exclusions(self.repo, self.report)
        self.assertEqual(self._meta(f"domain:{self.AUDITED_DOMAIN}"), before)

    def test_dry_run_main_makes_zero_writes(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        code = self._run_main(["--db", self.db_path, "--report",
                               _tmp_report(self.report)])
        self.assertEqual(code, 0)
        self.assertNotIn("exclusion",
                         self._meta(f"domain:{self.AUDITED_DOMAIN}"))

    def test_explicit_plan_flag_makes_zero_writes(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        before = self._meta(f"domain:{self.AUDITED_DOMAIN}")
        code = self._run_main(["--db", self.db_path, "--report",
                               _tmp_report(self.report), "--plan"])
        self.assertEqual(code, 0)
        self.assertEqual(self._meta(f"domain:{self.AUDITED_DOMAIN}"), before)
        self.assertNotIn("exclusion",
                         self._meta(f"domain:{self.AUDITED_DOMAIN}"))

    def test_plan_cannot_be_combined_with_apply(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        before = self._meta(f"domain:{self.AUDITED_DOMAIN}")
        with self.assertRaises(SystemExit):
            self._run_main(["--db", self.db_path, "--report",
                            _tmp_report(self.report), "--plan", "--apply"])
        self.assertEqual(self._meta(f"domain:{self.AUDITED_DOMAIN}"), before)

    def test_apply_writes_the_marker_with_provenance(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        report_path = _tmp_report(self.report)
        self.assertEqual(
            self._run_main(["--db", self.db_path, "--report", report_path,
                            "--apply"]), 0)
        marker = self._meta(f"domain:{self.AUDITED_DOMAIN}")["exclusion"]
        self.assertTrue(marker["active"])
        self.assertEqual(marker["mechanism"], excl.MECHANISM)
        self.assertEqual(marker["decision_source"], "requalify-full-v2.json")
        self.assertEqual(marker["report_generated_at"],
                         self.report["generated_at"])
        self.assertEqual(marker["bucket"], "denied_host")
        self.assertIn("not a station", marker["reason"])

    def test_apply_is_idempotent(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        report_path = _tmp_report(self.report)
        key = f"domain:{self.AUDITED_DOMAIN}"
        self._run_main(["--db", self.db_path, "--report", report_path,
                        "--apply"])
        after_first = self._meta(key)
        self._run_main(["--db", self.db_path, "--report", report_path,
                        "--apply"])
        self.assertEqual(self._meta(key), after_first)
        # A second apply plans nothing new to write.
        changes = excl.plan_exclusions(self.repo, self.report)
        self.assertEqual([c["action"] for c in changes],
                         ["already_applied"])

    def test_apply_refuses_a_record_outside_the_allowlist(self):
        # The record is a deterministic rejection, but it was never audited.
        self._patched_allowlist({"other-domain.org"})
        report = _report(_audited("never-audited.org"))
        report_path = _tmp_report(report)
        self.assertEqual(self._run_main(["--db", self.db_path, "--report",
                                     report_path, "--apply"]), 2)

    def test_apply_refuses_a_network_unverified_record(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        report = _report(_audited(self.AUDITED_DOMAIN,
                                  proposed_verdict="needs_review",
                                  proposed_kind="fetch_failed",
                                  determinism="network_required"))
        with self.assertRaises(excl.UnsafeRecord):
            excl.plan_exclusions(self.repo, report)

    def test_apply_refuses_a_transient_network_failure(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        report = _report(_audited(self.AUDITED_DOMAIN,
                                  is_transient_network_failure=True))
        with self.assertRaises(excl.UnsafeRecord):
            excl.plan_exclusions(self.repo, report)

    def test_apply_refuses_a_non_rejection_verdict(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        report = _report(_audited(self.AUDITED_DOMAIN,
                                  proposed_verdict="needs_review"))
        with self.assertRaises(excl.UnsafeRecord):
            excl.plan_exclusions(self.repo, report)

    def test_apply_refuses_a_domain_missing_from_the_report(self):
        self._patched_allowlist({"absent-from-report.org"})
        with self.assertRaises(excl.UnsafeRecord):
            excl.plan_exclusions(self.repo, self.report)

    def test_absent_stored_row_is_reported_not_written(self):
        self._patched_allowlist({"never-stored.org"})
        report = _report(_audited("never-stored.org"))
        changes = excl.plan_exclusions(self.repo, report)
        self.assertEqual([c["action"] for c in changes], ["absent"])

    def test_reverse_dry_run_makes_zero_writes(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        key = f"domain:{self.AUDITED_DOMAIN}"
        self._run_main(["--db", self.db_path, "--report",
                        _tmp_report(self.report), "--apply"])
        before = self._meta(key)
        self.assertEqual(
            self._run_main(["--db", self.db_path, "--reverse"]), 0)
        self.assertEqual(self._meta(key), before)

    def test_reverse_restores_the_prior_metadata(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        key = f"domain:{self.AUDITED_DOMAIN}"
        before = self._meta(key)
        self._run_main(["--db", self.db_path, "--report",
                        _tmp_report(self.report), "--apply"])
        self.assertIn("exclusion", self._meta(key))
        self.assertEqual(
            self._run_main(["--db", self.db_path, "--reverse", "--apply"]), 0)
        self.assertEqual(self._meta(key), before)
        self.assertNotIn("exclusion", self._meta(key))

    def test_reverse_ignores_a_foreign_exclusion_marker(self):
        key = f"domain:{self.AUDITED_DOMAIN}"
        self.repo.set_station_exclusion(key, {
            "active": True, "mechanism": "some_other_tool",
            "reason": "not ours"})
        reversals = excl.plan_reversals(self.repo)
        self.assertEqual(reversals, [])
        self.assertEqual(
            self._run_main(["--db", self.db_path, "--reverse", "--apply"]), 0)
        self.assertTrue(self._meta(key)["exclusion"]["active"])

    def test_reverse_never_deletes_the_station(self):
        self._patched_allowlist({self.AUDITED_DOMAIN})
        key = f"domain:{self.AUDITED_DOMAIN}"
        self._run_main(["--db", self.db_path, "--report",
                        _tmp_report(self.report), "--apply"])
        self._run_main(["--db", self.db_path, "--reverse", "--apply"])
        self.assertIsNotNone(self.repo.get_station(key))
        self.assertTrue(self.repo.get_station_contacts(key))

    def test_reverse_needs_no_report_file(self):
        # Reversal is driven by the stored markers, so it must stay available
        # when the audited report is not on disk.
        self._patched_allowlist({self.AUDITED_DOMAIN})
        key = f"domain:{self.AUDITED_DOMAIN}"
        self._run_main(["--db", self.db_path, "--report",
                        _tmp_report(self.report), "--apply"])
        self.assertIn("exclusion", self._meta(key))
        self.assertEqual(self._run_main(
            ["--db", self.db_path, "--report",
             os.path.join(self._tmp.name, "no-such-report.json"),
             "--reverse", "--apply"]), 0)
        self.assertNotIn("exclusion", self._meta(key))


class AuditedAllowlistTests(unittest.TestCase):
    """The shipped allowlist must stay exactly the reviewed 46."""

    def test_allowlist_has_the_audited_size_and_breakdown(self):
        kinds = {"denied_host": 9, "non_station_path": 26,
                 "non_station_info": 11}
        self.assertEqual(len(excl.AUDITED_DOMAINS), sum(kinds.values()))
        self.assertEqual(len(excl.AUDITED_DOMAINS), 46)

    def test_allowlist_contains_no_reserved_tld_fixture(self):
        for domain in excl.AUDITED_DOMAINS:
            self.assertFalse(domain.endswith((".example", ".test", ".invalid")),
                             f"{domain} is a dev fixture, not a real host")

    def test_allowlist_matches_the_audited_report_when_present(self):
        report_path = Path(__file__).resolve().parents[1] / \
            "requalify-full-v2.json"
        if not report_path.exists():
            self.skipTest("audited report not present in this checkout")
        report = excl.load_report(str(report_path))
        index = excl.report_index(report)
        for domain in excl.AUDITED_DOMAINS:
            self.assertIn(domain, index, f"{domain} missing from report")
            excl.check_report_record(domain, index[domain])

    def test_every_network_unverified_record_is_outside_the_allowlist(self):
        report_path = Path(__file__).resolve().parents[1] / \
            "requalify-full-v2.json"
        if not report_path.exists():
            self.skipTest("audited report not present in this checkout")
        report = excl.load_report(str(report_path))
        unreachable = {
            r["domain"] for r in report["records"]
            if r.get("determinism") == "network_required"}
        self.assertTrue(unreachable, "report has no network-unverified rows")
        self.assertEqual(unreachable & set(excl.AUDITED_DOMAINS), set())


def _tmp_report(report: dict) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(report, handle, ensure_ascii=False)
    handle.close()
    return handle.name


if __name__ == "__main__":
    unittest.main()