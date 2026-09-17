"""DJ data cleanup: assign a stored classification to every DJ row.

Candidate discovery surfaced pages that are not DJ profiles (event
listings, playlists, venue pages, search results, forum posts, editorial
articles). This script deterministically classifies every stored DJ row
from its own ``source_urls`` — no fetches, no LLM, no network:

  rejected            at least one source_url is a deterministic non-DJ
                      signature (event/ticketing/listing/playlist/search/
                      forum/editorial page) — the row is hidden from the
                      normal DJ listing forever (see ``exclude_rejected``).
  needs_verification  no deterministic signature (and by construction no
                      positive DJ evidence was stored) — the row stays
                      available for human review and is NEVER auto-promoted.

Guarantees:

  * idempotent  — a completed classification (verdict + kind + reason +
    evidence_url + evaluated_at) is never overwritten; a second run is a
    zero-change no-op.
  * reversible  — ``--reverse`` removes exactly the classifications written
    by this script run (tracked via the ``verification.cleanup`` marker),
    restoring the pre-cleanup verification blob.
  * minimal     — writes ONLY the ``djs.verification`` column via
    ``update_dj_verification()``. Stations, contacts, outreach records and
    all other DJ columns are never touched.
  * dry-run by default — without ``--apply`` nothing is written.
"""

from __future__ import annotations

import argparse
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

from discovery.djs.qualify import (  # noqa: E402
    KIND_NOT_A_DJ,
    VERDICT_REJECTED,
    classify_candidate,
)

RUN = "dj_classification_cleanup"
_VERDICT_NEEDS_VERIFICATION = "needs_verification"
_NO_SIGNATURE_REASON = (
    "no deterministic non-DJ signature and no stored positive DJ evidence — "
    "human review required before any use; never promoted automatically")


def target_classification(row: dict, now: str) -> dict | None:
    """Deterministic classification for one stored DJ row, or None to skip.

    Returns None when the row already carries the target classification
    (idempotency: a completed verdict is never overwritten).
    """
    verification = row.get("verification")
    current = (verification or {}).get("classification") \
        if isinstance(verification, dict) else None

    reject_reason = None
    reject_evidence = None
    sources = row.get("source_urls") or []
    for url in sources:
        try:
            qualification = classify_candidate(
                url=url, title=row.get("name") or "")
        except Exception:
            continue
        if qualification.verdict == VERDICT_REJECTED:
            reject_reason = qualification.reason
            reject_evidence = qualification.evidence_url
            break

    if reject_reason is not None:
        target = {
            "verdict": "rejected",
            "kind": KIND_NOT_A_DJ,
            "reason": reject_reason,
            "evidence_url": reject_evidence,
            "evaluated_at": now,
        }
        if isinstance(current, dict) and current.get("verdict") == "rejected" \
                and current.get("kind") == KIND_NOT_A_DJ \
                and current.get("reason") and current.get("evidence_url") \
                and current.get("evaluated_at"):
            return None
        return target

    target = {
        "verdict": _VERDICT_NEEDS_VERIFICATION,
        "kind": None,
        "reason": _NO_SIGNATURE_REASON,
        "evidence_url": sources[0] if sources else "",
        "evaluated_at": now,
    }
    if isinstance(current, dict) and current.get("verdict") == \
            _VERDICT_NEEDS_VERIFICATION \
            and current.get("reason") and current.get("evaluated_at"):
        return None
    return target


def plan_cleanup(repository, now: str = None) -> list[dict]:
    """Plan rows: one entry per DJ needing a new stored classification."""
    from discovery.models import utc_now_iso
    ts = now or utc_now_iso()
    rows, _ = repository.list_djs(limit=100000, offset=0)
    changes: list[dict] = []
    for row in rows:
        target = target_classification(row, ts)
        if target is None:
            continue
        changes.append({
            "dj_id": row["dj_id"],
            "name": row["name"],
            "sources": row.get("source_urls") or [],
            "target": target,
        })
    return changes


def apply_cleanup(repository, changes: list[dict]) -> int:
    """Persist classifications; returns the number of rows updated."""
    applied = 0
    for change in changes:
        dj = repository.get_dj(change["dj_id"])
        if dj is None:
            continue
        verification = dict(dj.get("verification") or {})
        verification["classification"] = change["target"]
        verification["cleanup"] = {
            "run": RUN,
            "applied_at": change["target"]["evaluated_at"],
        }
        if repository.update_dj_verification(change["dj_id"], verification):
            applied += 1
    return applied


def reverse_cleanup(repository) -> int:
    """Undo exactly the classifications written by this script run.

    Rows identified by the ``verification.cleanup.run`` marker have their
    ``classification`` and ``cleanup`` keys removed, restoring the blob that
    existed before cleanup.
    """
    rows, _ = repository.list_djs(limit=100000, offset=0)
    reverted = 0
    for row in rows:
        verification = row.get("verification")
        if not isinstance(verification, dict):
            continue
        marker = verification.get("cleanup") or {}
        if marker.get("run") != RUN:
            continue
        restored = dict(verification)
        restored.pop("classification", None)
        restored.pop("cleanup", None)
        if repository.update_dj_verification(row["dj_id"], restored):
            reverted += 1
    return reverted


def _print_verbose(changes: list[dict]) -> None:
    rejected = [c for c in changes if c["target"]["verdict"] == "rejected"]
    review = [c for c in changes if c["target"]["verdict"] != "rejected"]
    width = max((len(c["name"]) for c in changes), default=0)
    for title, group in (("REJECTED (NOT a DJ):", rejected),
                         ("NEEDS VERIFICATION (review):", review)):
        print(f"\n{title}")
        for c in group:
            name = (c["name"] or "").ljust(width)
            print(f"  {name}  {c['dj_id']}  {c['sources'][0] if c['sources'] else ''}")
            print(f"    -> {c['target']['reason']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.dj_classification_cleanup",
        description="Deterministically classify stored DJ records "
                    "(dry-run by default; use --apply to write).")
    parser.add_argument("--db", help="SQLite database path (overrides --dsn)")
    parser.add_argument("--dsn", default=os.environ.get("MIE_PG_DSN"),
                        help="PostgreSQL DSN (defaults to MIE_PG_DSN)")
    parser.add_argument("--apply", action="store_true",
                        help="persist the plan (default is dry-run)")
    parser.add_argument("--reverse", action="store_true",
                        help="undo the previous apply of this script")
    parser.add_argument("--json", action="store_true",
                        help="emit structured JSON instead of the table")
    args = parser.parse_args(argv)

    if args.db:
        from database.service import PersistenceService
        repository = PersistenceService(args.db)
    else:
        if not args.dsn:
            parser.error("provide --db or set MIE_PG_DSN")
        from database.pg_store import PostgresStorage
        repository = PostgresStorage(dsn=args.dsn)

    plan: list[dict] = []
    reverted = 0
    applied = 0
    try:
        if args.reverse:
            reverted = reverse_cleanup(repository)
        else:
            plan = plan_cleanup(repository)
            if args.apply:
                applied = apply_cleanup(repository, plan)
    finally:
        repository.close()

    if args.json:
        import json
        payload = {
            "mode": ("reverse" if args.reverse
                     else "apply" if args.apply else "dry-run"),
            "plan": [{
                "dj_id": c["dj_id"],
                "name": c["name"],
                "verdict": c["target"]["verdict"],
                "evidence_url": c["target"]["evidence_url"],
            } for c in plan],
            "applied": applied,
            "reverted": reverted,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    if args.reverse:
        print(f"Reverted {reverted} rows written by {RUN}.")
        return 0

    rejected = [c for c in plan if c["target"]["verdict"] == "rejected"]
    review = [c for c in plan if c["target"]["verdict"] != "rejected"]
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"DJ classification cleanup — {mode}")
    print(f"  plan size: {len(plan)} rows "
          f"({len(rejected)} rejected / {len(review)} needs verification)")
    _print_verbose(plan)
    if not args.apply:
        print("\nDRY RUN: nothing was written. Rerun with --apply to persist.")
    else:
        print(f"\nApplied {applied} classifications.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())