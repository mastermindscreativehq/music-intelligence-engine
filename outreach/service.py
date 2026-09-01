"""Outreach orchestration shared by every API adapter (Phase 9).

Mirrors the submission-service pattern: pure, adapter-agnostic functions
over a repository (PersistenceService) and an injected :class:`EmailProvider`.
Nothing here sends anything by itself — delivery is delegated to the
provider, and status transitions are recorded exactly as reported.

Hard rules (from outreach/README.md):
- No message is EVER marked ``sent`` because a mail client opened. Only a
  provider-confirmed send moves a record to ``sent``.
- Every action is appended to an attempts ledger (traceable history).
- Missing information is stored as NULL/omitted; nothing is invented.

Status vocabulary: draft | opened_in_email | sent | failed
"""

from __future__ import annotations

import uuid

from discovery.models import utc_now_iso

from outreach.providers import (
    DeliveryStatus,
    EmailProvider,
    LocalStubProvider,
)

OUTREACH_STATUSES = tuple(s.value for s in DeliveryStatus)

__all__ = [
    "DeliveryStatus", "OUTREACH_STATUSES", "create_outreach",
    "get_outreach", "list_outreach", "provider_for",
    "record_outreach_event",
]


def _oom_payload(row: dict, attempts: list[dict] | None = None) -> dict:
    """Normalize a single persisted record into the contract shape.

    Only fields actually present are kept; every value is copied verbatim
    (no fabrication). ``links`` are derived, never guessed.
    """
    return {
        "outreach_id": row["outreach_id"],
        "recipient": {
            "contact_uid": row["contact_uid"],
            "name": row.get("recipient_name"),
            "role": row.get("recipient_role"),
            "organization": row.get("organization"),
            "email": row["email"],
            "identity_key": row.get("identity_key"),
            "source_url": row.get("source_url"),
        },
        "track": row.get("track"),
        "context": row.get("context"),
        "subject": row.get("subject") or "",
        "message": row.get("message") or "",
        "from": row.get("from_email"),
        "sharing": row.get("sharing"),
        "status": row["status"],
        "provider": row.get("provider") or "local",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "attempts": attempts or [],
        "links": {"self": f"/api/v1/outreach/{row['outreach_id']}"},
    }


def create_outreach(repository, *, payload: dict,
                    provider: EmailProvider = None, now: str = None) -> dict:
    """Create a fresh outreach message record (status ``draft``).

    ``payload`` is the operator-supplied outreach (recipient, optional
    track/context/sharing, subject, message). Nothing is sent here.
    """
    provider = provider or LocalStubProvider()
    recipient = payload.get("recipient") or {}
    email = (recipient.get("email") or "").strip()
    if not email:
        raise ValueError("outreach_requires_email")

    outreach_id = "om_" + uuid.uuid4().hex[:16]
    ts = now or utc_now_iso()
    record = {
        "outreach_id": outreach_id,
        "contact_uid": str(recipient.get("contact_uid") or "").strip(),
        "identity_key": recipient.get("identity_key"),
        "recipient_name": recipient.get("name"),
        "recipient_role": recipient.get("role"),
        "organization": recipient.get("organization"),
        "email": email,
        "source_url": recipient.get("source_url"),
        "track_id": (payload.get("track") or {}).get("track_id"),
        "track": payload.get("track"),
        "context": payload.get("context"),
        "subject": str(payload.get("subject") or ""),
        "message": str(payload.get("message") or ""),
        "from_email": str(payload.get("from") or "").strip() or None,
        "sharing": payload.get("sharing"),
        "status": DeliveryStatus.DRAFT.value,
        "provider": provider.name,
        "created_at": ts,
        "updated_at": ts,
    }
    repository.save_outreach(record)
    return get_outreach(repository, outreach_id)


def get_outreach(repository, outreach_id: str) -> dict | None:
    row = repository.get_outreach(outreach_id)
    if row is None:
        return None
    attempts = repository.get_outreach_attempts(outreach_id)
    return _oom_payload(row, attempts)


def list_outreach(repository, *, limit=50, offset=0,
                  status: str | None = None) -> tuple[list, int]:
    if status is not None and status not in OUTREACH_STATUSES:
        raise ValueError(
            "'status' must be one of: " + ", ".join(OUTREACH_STATUSES))
    rows, total = repository.list_outreach(limit=limit, offset=offset,
                                           status=status)
    return [get_outreach(repository, r["outreach_id"]) for r in rows], total


def record_outreach_event(repository, outreach_id: str, *, event: str,
                          provider: EmailProvider = None,
                          meta: dict = None, now: str = None) -> dict:
    """Append a traceable attempt and transition status (recorded only).

    ``event`` is one of: opened_in_email | sent | failed.
    This function does not send; it records what the caller reports.
    """
    row = repository.get_outreach(outreach_id)
    if row is None:
        raise LookupError(f"unknown outreach {outreach_id!r}")
    if event not in OUTREACH_STATUSES or event == "draft":
        raise ValueError(f"invalid event {event!r}")

    provider = provider or provider_for(row["provider"])
    ts = now or utc_now_iso()
    attempt = {
        "outreach_id": outreach_id,
        "event": event,
        "provider": provider.name,
        "at": ts,
        "meta": meta or {},
    }
    repository.append_outreach_attempt(outreach_id, attempt)
    repository.set_outreach_status(outreach_id, event, ts)
    return get_outreach(repository, outreach_id)


def provider_for(name: str):
    from outreach.providers import provider_for as _pf
    return _pf(name)
