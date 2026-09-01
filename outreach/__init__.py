"""Phase 9: campaign & delivery layer (provider abstraction + outreach records).

- ``providers``: EmailProvider interface + status vocabulary + no-op stubs.
- ``service``: orchestration shared by every API adapter (stores outreach,
  records attempts/status transitions; never transmits by itself).
"""

from outreach.providers import (
    CAN_ATTACH,
    CAN_ERRORS,
    CAN_SEND,
    CAN_STATUS,
    DeliveryResult,
    DeliveryStatus,
    EmailEnvelope,
    EmailProvider,
    MailtoProvider,
    LocalStubProvider,
    default_provider,
    provider_for,
)

__all__ = [
    "CAN_ATTACH", "CAN_ERRORS", "CAN_SEND", "CAN_STATUS",
    "DeliveryResult", "DeliveryStatus", "EmailEnvelope", "EmailProvider",
    "MailtoProvider", "LocalStubProvider", "default_provider",
    "provider_for",
]
