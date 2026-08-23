"""Verification workflow (Phase 5).

Turns cross-source evidence into EXPLICIT verification states — never
vague booleans. States follow docs/data-model.md ("Verification Result:
status unverified|verified|failed|stale") extended with the two conflict
distinctions the comparison layer can prove:

    UNVERIFIED   default; claim has support but not enough to verify
    VERIFIED     >=2 independent sources agree (verifier='code')
    CONFLICTING  sources disagree; both sides preserved, no auto-winner
    UNSUPPORTED  a value is stored without any usable provenance
    STALE        previously verified but older than the freshness budget
    FAILED       an explicit verification attempt contradicted the claim

Hard guarantees:

- Evidence-driven only. A state changes because provenance proves it,
  never because code feels confident. Absent claims stay absent
  (UNKNOWN by omission) — nothing is invented.
- Results are append-only dicts mirroring the documented entity model:
  claim, status, method, verifier ('code'|'human'), evidence references,
  timestamps.
- ``apply_verification`` mutates ONLY lifecycle fields (verified_at /
  raw_metadata summary). Fact values are never rewritten here; conflict
  resolution is deliberately left to humans or newer evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from discovery.models import utc_now_iso
from discovery.events import get_logger, log_event

from enrichment.compare import (
    CONFLICTING as COMPARE_CONFLICTING,
    CORROBORATED as COMPARE_CORROBORATED,
    SINGLE_SOURCE as COMPARE_SINGLE_SOURCE,
    compare_record,
)

EVENT_VERIFICATION_STARTED = "verification_started"
EVENT_CLAIM_VERIFIED = "claim_verified"
EVENT_CLAIM_CONFLICTING = "claim_conflicting"
EVENT_VERIFICATION_COMPLETED = "verification_completed"

UNVERIFIED = "unverified"
VERIFIED = "verified"
CONFLICTING = "conflicting"
UNSUPPORTED = "unsupported"
STALE = "stale"
FAILED = "failed"

VERIFIER_CODE = "code"
VERIFIER_HUMAN = "human"          # reserved; humans verify in later phases

DEFAULT_MAX_AGE_DAYS = 90


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def age_days(checked_at: str, now: datetime) -> float | None:
    """Age of a timestamp in days, or None when unparsable."""
    parsed = parse_iso(checked_at)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 86400.0)


def is_stale(verified_at: str | None, now: datetime,
             max_age_days: int) -> bool:
    """True when a previous verification exceeds the freshness budget."""
    if not verified_at:
        return False
    age = age_days(verified_at, now)
    return age is not None and age > max_age_days


def verify_record(record: dict, *, now: datetime | None = None,
                  max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> list[dict]:
    """Produce append-only verification results for one record.

    Read-only: the record is not modified. Use :func:`apply_verification`
    to fold lifecycle fields forward afterwards.
    """
    now = now or datetime.now(timezone.utc)
    comparison = compare_record(record)
    results: list[dict] = []
    checked_at = utc_now_iso()

    station_stale = is_stale(record.get("last_verified_at"), now,
                             max_age_days)

    for entry in comparison.get("claims") or []:
        outcome = entry["outcome"]
        base = {
            "claim": entry["claim"],
            "subject_id": record.get("station_id") or record.get("domain"),
            "evidence": [dict(v) for v in entry["values"]],
            "reasons": list(entry["reasons"]),
            "checked_at": checked_at,
            "verifier": VERIFIER_CODE,
        }
        if outcome == COMPARE_CORROBORATED:
            status = STALE if station_stale else VERIFIED
            base.update(status=status,
                        method="source_comparison",
                        reasons=list(entry["reasons"]) + [
                            "verification basis: independent-source "
                            "corroboration"])
            if station_stale:
                base["reasons"].append(
                    "previous verification exceeded freshness budget; "
                    "re-corroborated now")
        elif outcome == COMPARE_CONFLICTING:
            base.update(status=CONFLICTING,
                        method="source_comparison")
        elif outcome == COMPARE_SINGLE_SOURCE:
            base.update(status=UNVERIFIED,
                        method="source_comparison",
                        reasons=list(entry["reasons"]) + [
                            "insufficient independent sources to verify"])
        else:
            base.update(status=UNVERIFIED, method="source_comparison")
        results.append(base)

    results.extend(_unsupported_contact_claims(record))

    log_event(get_logger("mie.verification"),
              EVENT_VERIFICATION_STARTED, claims=len(results))
    for result in results:
        if result["status"] == VERIFIED:
            log_event(get_logger("mie.verification"),
                      EVENT_CLAIM_VERIFIED, claim=result["claim"])
        elif result["status"] == CONFLICTING:
            log_event(get_logger("mie.verification"),
                      EVENT_CLAIM_CONFLICTING, claim=result["claim"])
    return results


def _unsupported_contact_claims(record: dict) -> list[dict]:
    """Flag contact values stored WITHOUT provenance as unsupported."""
    results: list[dict] = []
    for index, contact in enumerate(record.get("contacts") or []):
        if not isinstance(contact, dict):
            continue
        email = contact.get("email")
        if not email:
            continue
        backed = any(
            isinstance(fact, dict)
            and fact.get("value") == email
            and fact.get("source_url")
            for fact in record.get("emails") or [])
        if not contact.get("provenance") and not backed:
            results.append({
                "claim": f"contacts[{index}].email",
                "subject_id": record.get("station_id")
                or record.get("domain"),
                "status": UNSUPPORTED,
                "method": "provenance_audit",
                "verifier": VERIFIER_CODE,
                "evidence": [],
                "reasons": ["stored value carries no provenance reference"],
                "checked_at": utc_now_iso(),
            })
    return results


def apply_verification(record: dict, results: list[dict]) -> dict:
    """Fold verification results into lifecycle fields ONLY.

    Sets ``last_verified_at`` / per-contact ``verified_at`` when a claim
    became VERIFIED, records the summary under ``raw_metadata``, and
    downgrades to STALE markers only via explicit STALE results. Fact
    values are never touched.
    """
    verified_claims = [r for r in results if r.get("status") == VERIFIED]
    stale_claims = [r for r in results if r.get("status") == STALE]
    conflicting_claims = [r for r in results
                          if r.get("status") == CONFLICTING]
    if not results:
        return record

    metadata = dict(record.get("raw_metadata") or {})
    verification_meta = dict(metadata.get("verification") or {})
    history = list(verification_meta.get("history") or [])
    history.append({
        "checked_at": utc_now_iso(),
        "claims_checked": len(results),
        "verified": len(verified_claims),
        "conflicting": len(conflicting_claims),
        "stale": len(stale_claims),
    })
    verification_meta["history"] = history[-20:]
    verification_meta["last_result"] = {
        "verified": len(verified_claims),
        "conflicting": len(conflicting_claims),
        "stale": len(stale_claims),
        "unverified": sum(1 for r in results
                          if r.get("status") == UNVERIFIED),
        "unsupported": sum(1 for r in results
                           if r.get("status") == UNSUPPORTED),
    }
    metadata["verification"] = verification_meta
    record["raw_metadata"] = metadata

    if verified_claims:
        record["last_verified_at"] = utc_now_iso()
        for index, contact in enumerate(record.get("contacts") or []):
            if not isinstance(contact, dict):
                continue
            contact_verified = any(
                r["claim"].startswith(f"contacts[{index}]")
                for r in verified_claims)
            if contact_verified and not contact.get("verified_at"):
                contact["verified_at"] = utc_now_iso()
    return record


def verify_records(records: list[dict], *, now: datetime | None = None,
                   max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                   apply: bool = False) -> dict:
    """Verify every record; returns a run report (and optionally applies)."""
    started = utc_now_iso()
    all_results: list[dict] = []
    per_record: list[dict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        results = verify_record(record, now=now, max_age_days=max_age_days)
        all_results.extend(results)
        if apply:
            apply_verification(record, results)
        per_record.append({
            "subject_id": record.get("station_id") or record.get("domain"),
            "results": results,
        })
    summary = {status: 0 for status in
               (UNVERIFIED, VERIFIED, CONFLICTING, UNSUPPORTED, STALE,
                FAILED)}
    for result in all_results:
        summary[result["status"]] += 1
    completed = utc_now_iso()
    log_event(get_logger("mie.verification"), EVENT_VERIFICATION_COMPLETED,
              records=len(per_record), **summary)
    return {
        "started_at": started,
        "completed_at": completed,
        "records": per_record,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_records(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        records = data.get("records")
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)]
    raise ValueError(
        "input must be a JSON array of records or an object with a "
        "'records' array")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m enrichment.verify",
        description=(
            "Cross-source verification of enriched intelligence records. "
            "Offline by default; reports explicit verification states "
            "without inventing facts."))
    parser.add_argument("--input", required=True,
                        help="path to enriched intelligence JSON")
    parser.add_argument("--output", default="-",
                        help="report output path (default stdout)")
    parser.add_argument("--max-age-days", type=int,
                        default=DEFAULT_MAX_AGE_DAYS,
                        help="freshness budget before a verification goes "
                             "stale (default 90)")
    parser.add_argument("--apply-out", default=None,
                        help="optional path to write records with applied "
                             "lifecycle fields (last_verified_at etc.)")
    args = parser.parse_args(argv)

    records = _load_records(args.input)
    report = verify_records(records, max_age_days=args.max_age_days,
                            apply=bool(args.apply_out))
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output == "-":
        sys.stdout.write(payload + "\n")
    else:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    if args.apply_out:
        with open(args.apply_out, "w", encoding="utf-8") as handle:
            json.dump({"records": records}, handle, indent=2,
                      ensure_ascii=False)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
