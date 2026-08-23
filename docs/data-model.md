# Database Architecture (Phase 1 documentation)

PostgreSQL-compatible, Supabase-ready. This document defines the **intended entity
model**. No DDL is implemented in Phase 1; migrations arrive in Phase 6.

## 1. Design goals

- Reusable across target types: radio is `organization_type = 'radio_station'`, not a
  bespoke schema.
- Full provenance: every fact traceable to a source, method, confidence, and timestamp.
- Separation of *found* vs *verified* vs *confident* vs *relevant*.
- Outreach safety built into the model (suppression, opt-out, approval state).

## 2. Core entities

### Organization
An industry entity. `type_id` → OrganizationType. Fields include name, normalized domain,
website, country/state/region/city, description, status, aggregate confidence.

### Organization Type
Lookup/enum: `radio_station`, `playlist_curator`, `dj`, `blog`, `publication`,
`label`, `a_and_r`, `festival`, `event`, `influencer`, `creator`, `other`.

### Contact
A person or role associated with an organization (`org_id`). Name, role/title, status.

### Contact Method
Reachable channel attached to an organization and/or contact:
`email`, `phone`, `social_profile`, `submission_url`, `contact_form`, `address`.
Carries its own value, normalization, confidence, and verification state.

### Source
Where intelligence came from: URL, source type (`official_website`, `contact_page`,
`submission_page`, `public_directory`, `search_result`, `social_profile`),
retrieved-at timestamp, content snapshot reference.

### Discovery Event
How an organization/contact was first found: source, discovery method, query/context,
timestamp. Append-only history — never overwritten.

### Enrichment Result
Derived information: field enriched, value, method (`rule` | `llm` | `manual`),
model version where applicable, confidence, input source references. Append-only so
re-enrichment never destroys history.

### Verification Result
Verification attempts per claim/method: status (`unverified`, `verified`,
`failed`, `stale`, `conflicting`, `unsupported`; the last two are Phase 5
extensions — sources disagree with both sides preserved, and a stored value
without provenance respectively), method used, evidence reference, verifier
(code/human), timestamp.

### Campaign
An outreach campaign: purpose, template reference, music/submission links, status
(`draft`, `in_review`, `approved`, `sending`, `paused`, `completed`), created-by,
approval records.

### Campaign Recipient
A contact included in a campaign, with per-recipient selection reason, message draft,
review/approval state, and send state. The unit of human review.

### Message
Generated outreach content: recipient, generation method + model + prompt version,
draft text, final approved text, approved-by, approved-at.

### Submission
An actual music submission: track/file reference, submission link, campaign recipient,
sent-at, external status.

### Delivery Event
Provider events per recipient/message: `queued`, `sent`, `delivered`, `bounced`,
`failed`, timestamps, provider response metadata. Append-only.

### Suppression / Opt-Out
Identity-based blocklist (email/domain/org) with reason
(`opt_out`, `bounce_hard`, `complaint`, `manual`) and timestamp. Checked before any send.

## 3. Relationships (summary)

```
OrganizationType 1──* Organization 1──* Contact 1──* ContactMethod
Organization 1──* Source / DiscoveryEvent / EnrichmentResult / VerificationResult
Campaign 1──* CampaignRecipient *──1 Contact
CampaignRecipient 1──* Message 1──* DeliveryEvent
CampaignRecipient 1──0..1 Submission
Suppression checked against ContactMethod/Organization before any send
```

## 4. Radio station mapping

The future radio intelligence record maps onto the generic model:

| Radio concept           | Generic home                                        |
|-------------------------|-----------------------------------------------------|
| station name            | Organization.name                                   |
| website                 | Organization.website (+ normalized domain)          |
| country/state/region/city | Organization location fields                      |
| station format / genre  | EnrichmentResult classifications on Organization    |
| contact name / role     | Contact.name / Contact.role                         |
| email / phone / social  | ContactMethod rows                                  |
| submission URL + instructions | ContactMethod(type=`submission_url`) + enrichment note |
| source URL / type       | Source                                              |
| discovery date          | DiscoveryEvent                                      |
| last verification       | VerificationResult                                  |
| email / contact / org confidence | Confidence fields on respective entities   |
| outreach status         | CampaignRecipient / organization status             |
| notes                   | freeform annotation                                 |

Radio-specific display can be composed in the backend/frontend without new tables.

## 5. Deduplication strategy (documented, not yet implemented)

Signals, compared after normalization:

1. normalized domain (strongest organizational signal)
2. normalized organization/station name (exact → then fuzzy)
3. email address (normalized local+domain)
4. phone (E.164 digits)
5. external identifiers where available

Planned staging:

- **Stage A (Phase 5):** exact matches on normalized keys; merge with provenance retained;
  conflicts kept as alternates rather than destroyed.
- **Stage B (later):** fuzzy similarity scoring (name + location + domain proximity) with
  human confirmation for ambiguous merges.
- Merging never deletes data: loser records are linked as duplicates, sources preserved.

## 6. Conventions

- UUID primary keys; `created_at`/`updated_at` on all tables.
- Append-only tables: `source`, `discovery_event`, `enrichment_result`,
  `verification_result`, `delivery_event`.
- All LLM-derived values carry `method`, model identifier, and prompt version.
- Soft-delete/status flags over destructive deletes.

## 7. Phase 2 record mapping (implemented JSON → future PostgreSQL)

Phase 2 produces normalized records as JSON-mappable dicts
(`discovery/radio/schema.py`). Future table mapping:

| StationRecord / ContactRecord field | Future home                                   |
|-------------------------------------|-----------------------------------------------|
| `StationRecord`                     | Organization row (`organization_type='radio_station'`) |
| `station_type`, `classification_*`  | EnrichmentResult rows (method=`rule`)         |
| `ContactRecord`                     | Contact row (+ ContactMethod type=`email`)    |
| `emails[]` (Fact dicts)             | ContactMethod rows + Source references        |
| Fact: value/source_url/source_type/method/discovered_at/also_seen_at | Source + EnrichmentResult provenance columns |
| `source_urls[]`                     | Source rows                                   |
| discovery/observation timestamps    | DiscoveryEvent rows                           |
| `confidence_score` + `confidence_reasons` | confidence columns (explainable)        |
| `submission_url`, `contact_url`, `programming_url` | ContactMethod(type=...) or Organization fields |
| `social_urls`                       | ContactMethod(type=`social_profile`)          |
| `raw_metadata`                      | JSONB column                                  |

No migration is created in Phase 2; this mapping is the contract for Phase 6.
