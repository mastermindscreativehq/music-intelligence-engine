"""Station exclusion: hide audited non-stations from selection, reversibly.

An operator audit of ``requalify-full-v2.json`` classified every stored
station record and identified 46 records that are deterministically NOT
radio stations (denied hosts such as ``dol.gov``, and non-station paths or
info pages such as ``nytimes.com``). Those 46 are marked via
``raw_metadata.exclusion`` so they disappear from the normal station listing
and from opportunity/outreach selection.

The other 545 records are left exactly as they are: 110 need human review and
435 must not be touched at all. The 150 records the live run could never
reach carry ``determinism == "network_required"`` — a network failure is never
a rejection, so they are structurally ineligible here.

Guarantees:

  * allowlisted  — only the 46 audited domains below are ever written. Any
    other record (including a report-verdict mismatch or a network-unverified
    record) is refused; the run aborts rather than writing a partial set.
  * non-destructive — writes ONLY the ``raw_metadata.exclusion`` key via
    ``set_station_exclusion()``. No DELETE, no contact/submission/fetch/
    outreach row touched, and ``raw_metadata.qualification`` is preserved
    verbatim. Every station record stays fully readable via ``get_station``.
  * reversible — ``--reverse`` removes only the markers written by this
    mechanism (tracked via ``exclusion.mechanism``), restoring the previous
    state exactly.
  * idempotent — a record already carrying this mechanism's active marker is
    never rewritten, so a second ``--apply`` is a zero-write no-op.
  * dry-run by default — without ``--apply`` nothing is written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Load .env so MIE_PG_DSN is available without a shell export.
_env_path = os.path.join(_ROOT, ".env")
if os.path.exists(_env_path):
    for line in open(_env_path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

MECHANISM = "station_exclusion_apply"

# The audited safe set, exactly as reviewed: 9 denied hosts, 26 non-station
# paths, 11 non-station info pages. Anything outside this set is never written.
AUDITED_DOMAINS = frozenset({
    # denied_host (9)
    "dol.gov", "dot.gov", "drought.gov", "encyclopedia.com", "ny.gov",
    "senate.gov", "ttb.gov", "uscourts.gov", "usda.gov",
    # non_station_path (26)
    "aeq.es", "billscorzari.com", "footprintnetwork.org", "frontiersin.org",
    "gettyimages.com", "godtube.com", "januvemusic.com", "jazztimes.com",
    "legendarymix.com", "mhanational.org", "mla.org",
    "modernreformation.org", "musicinafrica.net", "nami.org",
    "nationaljazzarchive.org.uk", "nytimes.com",
    "organicmusicmarketing.com", "psychologytoday.com",
    "realchicksrock.com", "rode.com", "saskmusic.org", "songstuff.com",
    "songwriting.net", "stephenallenmusic.com", "threads.com",
    "radiopromo.io",
    # non_station_info (11)
    "austinfilmschool.org", "betheremichigan.com", "countryradioseminar.com",
    "groover.co", "himalayas.app", "lutzmultimedia.com", "meet-eric.com",
    "musicreviewworld.com", "planetarygroup.com",
    "thebluegrassjamboree.com", "xpressmagazine.org",
})

DEFAULT_REPORT = os.path.join(_ROOT, "requalify-full-v2.json")

# A rejection is only ever deterministic evidence; a live run that could not
# reach the site is never one.
_DETERMINISM_REFUSED = "network_required"


class UnsafeRecord(Exception):
    """A record that must never be excluded by this mechanism."""


def load_report(path: str) -> dict:
    """Load the audited requalification report."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def report_index(report: dict) -> dict:
    """Map domain -> report record for every audited record."""
    index: dict[str, dict] = {}
    for record in report.get("records") or []:
        domain = record.get("domain")
        if domain:
            index[domain] = record
    return index


def check_report_record(domain: str, record: dict) -> dict:
    """Validate one report record is a safe, deterministic rejection.

    Raises ``UnsafeRecord`` unless the record is a non-transient,
    deterministically-reached ``rejected`` verdict. This is the second gate
    behind the allowlist: even an allowlisted domain is refused if the report
    says it was merely unreachable.
    """
    if record.get("proposed_verdict") != "rejected":
        raise UnsafeRecord(
            f"{domain}: report verdict is "
            f"{record.get('proposed_verdict')!r}, not 'rejected'")
    determinism = record.get("determinism")
    if determinism == _DETERMINISM_REFUSED:
        raise UnsafeRecord(
            f"{domain}: network-unverified record "
            f"(determinism={determinism!r}); a network failure is never a "
            f"rejection")
    if record.get("is_transient_network_failure"):
        raise UnsafeRecord(
            f"{domain}: transient network failure; never a rejection")
    if not record.get("reason"):
        raise UnsafeRecord(f"{domain}: report carries no reason to audit")
    return {
        "kind": record.get("proposed_kind"),
        "reason": record.get("reason"),
        "determinism": determinism,
        "station_id": record.get("station_id"),
        "website": record.get("website"),
    }


def target_exclusion(report_record: dict, report_meta: dict, now: str) -> dict:
    """The ``raw_metadata.exclusion`` payload for one audited record.

    Carries the audit provenance (which report, which bucket, which reason)
    plus the mechanism marker that makes ``--reverse`` able to undo exactly
    what this script wrote.
    """
    return {
        "active": True,
        "mechanism": MECHANISM,
        "reason": report_record["reason"],
        "bucket": report_record["kind"],
        "decision_source": "requalify-full-v2.json",
        "report_generated_at": report_meta.get("generated_at"),
        "station_id": report_record.get("station_id"),
        "applied_at": now,
    }


def _already_applied(row: dict) -> bool:
    meta = row.get("raw_metadata")
    marker = meta.get("exclusion") if isinstance(meta, dict) else None
    return (isinstance(marker, dict) and marker.get("active") is True
            and marker.get("mechanism") == MECHANISM)


def plan_exclusions(repository, report: dict,
                    now: str = None) -> list[dict]:
    """One planned change per stored station that is safe to exclude.

    Every allowlisted domain is resolved against the report and the stored
    row; an unknown domain, a report mismatch, or a missing stored row is
    reported rather than silently skipped.
    """
    from discovery.models import utc_now_iso
    ts = now or utc_now_iso()
    meta = {"generated_at": report.get("generated_at")}
    index = report_index(report)

    changes: list[dict] = []
    for domain in sorted(AUDITED_DOMAINS):
        record = index.get(domain)
        if record is None:
            raise UnsafeRecord(
                f"{domain}: audited domain is absent from the report")
        report_record = check_report_record(domain, record)
        row = repository.get_station(report_record.get("station_id")
                                     or f"domain:{domain}")
        if row is None:
            changes.append({
                "domain": domain,
                "station_id": report_record.get("station_id"),
                "action": "absent",
                "detail": "no stored row",
                "target": None,
            })
            continue
        if _already_applied(row):
            changes.append({
                "domain": domain,
                "station_id": row["identity_key"],
                "name": row.get("name"),
                "action": "already_applied",
                "detail": report_record["kind"],
                "target": None,
            })
            continue
        changes.append({
            "domain": domain,
            "station_id": row["identity_key"],
            "name": row.get("name"),
            "action": "exclude",
            "detail": report_record["kind"],
            "target": target_exclusion(report_record, meta, ts),
        })
    return changes


def apply_exclusions(repository, changes: list[dict]) -> int:
    """Write the exclusion marker for each planned change."""
    applied = 0
    for change in changes:
        if change["action"] != "exclude":
            continue
        if repository.set_station_exclusion(change["station_id"],
                                            change["target"]):
            applied += 1
    return applied


def plan_reversals(repository) -> list[dict]:
    """Stations carrying an active exclusion written by this mechanism."""
    rows, _total = repository.list_stations(limit=100000, offset=0)
    reversals: list[dict] = []
    for row in rows:
        meta = row.get("raw_metadata")
        marker = meta.get("exclusion") if isinstance(meta, dict) else None
        if not isinstance(marker, dict):
            continue
        if marker.get("mechanism") != MECHANISM:
            continue
        reversals.append({
            "station_id": row["identity_key"],
            "name": row.get("name"),
            "active": marker.get("active"),
        })
    return reversals


def reverse_exclusions(repository, reversals: list[dict]) -> int:
    """Remove only the markers this mechanism wrote; leave others alone."""
    reverted = 0
    for change in reversals:
        if repository.set_station_exclusion(change["station_id"], None):
            reverted += 1
    return reverted


def _print_table(changes: list[dict]) -> None:
    width = max((len(c.get("name") or "") for c in changes), default=0)
    for action in ("exclude", "already_applied", "absent"):
        group = [c for c in changes if c["action"] == action]
        if not group:
            continue
        print(f"\n{action.upper()} ({len(group)}):")
        for change in group:
            name = (change.get("name") or change["domain"]).ljust(width)
            print(f"  {name}  {change['station_id']}  {change['domain']}"
                  f"  [{change['detail']}]")
    refused = [c for c in changes if c["action"] == "refused"]
    if refused:
        print(f"\nREFUSED ({len(refused)}):")
        for change in refused:
            print(f"  {change['domain']}  {change.get('detail')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/station_exclusion_apply.py",
        description="Apply/reverse the audited 46-station exclusion "
                    "(dry-run by default; use --apply to write).")
    parser.add_argument("--db", help="SQLite database path (overrides --dsn)")
    parser.add_argument("--dsn", default=os.environ.get("MIE_PG_DSN"),
                        help="PostgreSQL DSN (defaults to MIE_PG_DSN)")
    parser.add_argument("--report", default=DEFAULT_REPORT,
                        help="audited requalification report JSON")
    parser.add_argument("--plan", action="store_true",
                        help="explicit dry-run: report the plan, write nothing "
                             "(this is also the default)")
    parser.add_argument("--apply", action="store_true",
                        help="persist the plan (default is dry-run)")
    parser.add_argument("--reverse", action="store_true",
                        help="undo exclusions written by this mechanism")
    parser.add_argument("--json", action="store_true",
                        help="emit structured JSON instead of the table")
    args = parser.parse_args(argv)

    if args.plan and args.apply:
        parser.error("--plan is a dry run and cannot be combined with --apply")
    if args.plan and args.reverse:
        parser.error("--plan and --reverse are mutually exclusive")

    if args.db:
        from database.service import PersistenceService
        repository = PersistenceService(args.db)
    else:
        if not args.dsn:
            parser.error("provide --db or set MIE_PG_DSN")
        from database.pg_store import PostgresStorage
        repository = PostgresStorage(dsn=args.dsn)

    try:
        if args.reverse:
            # Reversal is driven entirely by the stored markers, never by the
            # report, so it stays available even if the report file is gone.
            reversals = plan_reversals(repository)
            reverted = reverse_exclusions(repository, reversals) \
                if args.apply else 0
            changes, applied = [], 0
        else:
            report = load_report(args.report)
            changes = plan_exclusions(repository, report)
            reversals = []
            applied = apply_exclusions(repository, changes) \
                if args.apply else 0
            reverted = 0
    except UnsafeRecord as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        repository.close()

    excludes = [c for c in changes if c["action"] == "exclude"]
    already = [c for c in changes if c["action"] == "already_applied"]
    absent = [c for c in changes if c["action"] == "absent"]

    if args.json:
        payload = {
            "mode": ("reverse" if args.reverse
                     else "apply" if args.apply else "dry-run"),
            "plan": [{
                "station_id": c["station_id"],
                "name": c.get("name"),
                "domain": c["domain"],
                "action": c["action"],
                "bucket": c["detail"],
            } for c in changes],
            "reverse": [{
                "station_id": r["station_id"],
                "name": r["name"],
                "active": r["active"],
            } for r in reversals],
            "applied": applied,
            "reverted": reverted,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if args.reverse:
        if not args.apply:
            print(f"Station exclusion reverse — DRY-RUN ({len(reversals)} "
                  f"markers written by {MECHANISM} would be removed)")
            _print_table([{"domain": r["station_id"],
                           "station_id": r["station_id"],
                           "name": r["name"],
                           "action": "exclude",
                           "detail": "marker"} for r in reversals])
            print("\nDRY RUN: nothing was written. Rerun with --apply.")
        else:
            print(f"Reverted {reverted} exclusions written by {MECHANISM}.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Station exclusion apply — {mode}")
    print(f"  audited allowlist: {len(AUDITED_DOMAINS)} domains")
    print(f"  to exclude: {len(excludes)}")
    print(f"  already applied: {len(already)}")
    print(f"  no stored row: {len(absent)}")
    _print_table(changes)
    if not args.apply:
        print("\nDRY RUN: nothing was written. Rerun with --apply to persist.")
    else:
        print(f"\nApplied {applied} exclusions. Reverse with --reverse --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())