# database/

PostgreSQL-compatible persistence layer, suitable for Supabase (Phase 6+).

The full intended entity model is documented in [`docs/data-model.md`](../docs/data-model.md):
Organization, OrganizationType, Contact, ContactMethod, Source, DiscoveryEvent,
EnrichmentResult, VerificationResult, Campaign, CampaignRecipient, Message, Submission,
DeliveryEvent, Suppression/OptOut.

Key conventions: UUID keys, append-only history tables, provenance on every fact,
soft deletes. Schema/migrations will be created here in Phase 6 — no DDL exists yet.
