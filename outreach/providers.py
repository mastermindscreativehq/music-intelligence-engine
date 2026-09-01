"""Email delivery provider abstraction (Phase 9, pre-authentication).

Design:
- ``EmailProvider`` is the interface every delivery backend must satisfy.
- Providers report *capabilities* (``can_send``, ``can_attach``, ...) so the
  UI only offers what a provider really supports (e.g. attaching a local MP3
  over a plain mailto is impossible and never offered here).
- ``send()`` returns a :-class:`DeliveryResult` with an explicit status and
  error; it is NEVER assumed to have succeeded. The caller must not mark an
  outreach as ``sent`` merely because a mail client opened — that transition
  is only taken when a provider confirms delivery.

Status vocabulary (shared with the outreach records layer):
    draft | opened_in_email | sent | failed

No provider credentials are configured yet; every concrete provider here is
a local/no-op stub. Real providers (Gmail/Microsoft/SMTP/transactional) can
be added without touching callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class DeliveryStatus(str, Enum):
    DRAFT = "draft"
    OPENED_IN_EMAIL = "opened_in_email"
    SENT = "sent"
    FAILED = "failed"


#: Provider capability flags
CAN_SEND = "send"
CAN_ATTACH = "attach"
CAN_STATUS = "status"
CAN_ERRORS = "errors"


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a provider.send() call. Existence here does NOT imply
    delivery: ``ok`` is True only when the provider confirmed sending."""
    ok: bool
    status: DeliveryStatus
    provider: str = "unknown"
    message_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status.value,
            "provider": self.provider,
            "message_id": self.message_id,
            "error": self.error,
        }


@dataclass
class EmailEnvelope:
    """A fully-specified message a provider may send."""
    to: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    from_: str | None = None
    attachments: list[dict] = field(default_factory=list)  # [{name, bytes|path}]


class EmailProvider(Protocol):
    """Interface contract for any delivery backend."""
    name: str

    def capabilities(self) -> set[str]: ...

    def can(self, capability: str) -> bool: ...

    def send(self, envelope: EmailEnvelope) -> DeliveryResult: ...

    def status_of(self, message_id: str) -> DeliveryStatus | None: ...


class LocalStubProvider:
    """Records what WOULD have been sent, but never transmits anything.

    Safe default while no SMTP/Gmail/transactional credentials exist.
    ``send()`` is deliberately withheld (raises) so nothing is ever marked
    sent; a caller must resolve a provider that can actually deliver.
    """

    name = "local"

    def capabilities(self) -> set[str]:
        return {CAN_SEND}

    def can(self, capability: str) -> bool:
        return capability in self.capabilities()

    def send(self, envelope: EmailEnvelope) -> DeliveryResult:
        # Never claims delivery. A local controller has no transport, so
        # treat any send request as unsupported rather than as success.
        raise NotImplementedError(
            "local stub provider cannot transmit; configure a real "
            "provider (gmail/smtp/transactional) before sending")

    def status_of(self, message_id: str) -> DeliveryStatus | None:
        return None


class MailtoProvider:
    """Provider for 'open in the user's own mail client'.

    This is a REAL handoff: it produces the mailto: link the operator opens.
    It cannot attach files and cannot track true delivery, so capability
    ``attachments`` is NOT offered and ``status_of`` stays unknown; the
    record transitions to ``opened_in_email`` at most.
    """

    name = "mailto"

    def capabilities(self) -> set[str]:
        return {CAN_SEND, CAN_ERRORS}

    def can(self, capability: str) -> bool:
        return capability in self.capabilities()

    def send(self, envelope: EmailEnvelope) -> DeliveryResult:
        to = [e for e in envelope.to if e]
        if not to:
            return DeliveryResult(False, DeliveryStatus.FAILED, self.name,
                                  error="no recipient email")
        params = []
        if envelope.subject:
            params.append(("subject", envelope.subject))
        if envelope.body:
            params.append(("body", envelope.body))
        query = "&".join(
            f"{k}={url_quote(v)}" for k, v in params)
        link = f"mailto:{','.join(to)}" + (f"?{query}" if query else "")
        # Returning a non-standard payload in envelope.attachments[0] would
        # imply attachable; instead the link is surfaced via error=None and
        # a synthetic message_id carrying the href for the UI.
        return DeliveryResult(True, DeliveryStatus.OPENED_IN_EMAIL,
                              self.name, message_id=link)

    def status_of(self, message_id: str) -> DeliveryStatus | None:
        # A mail client handoff has no delivery feedback channel.
        return None


def url_quote(value: str) -> str:
    """Percent-encode for a mailto query string (space -> %20 etc.)."""
    from urllib.parse import quote
    return quote(value, safe="")


def default_provider() -> EmailProvider:
    """The provider used when none is configured: non-sending local stub."""
    return LocalStubProvider()


def provider_for(name: str | None) -> EmailProvider:
    """Return a provider by name; unknown names fall back to the stub."""
    if name == "mailto":
        return MailtoProvider()
    return default_provider()
