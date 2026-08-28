"""Phase 2 live enrichment test script.

Reads the 3 existing organizations from PostgreSQL, runs live enrichment
with staff-page extraction, persists enriched results back, and lists
the final state.  Safe to re-run: all writes are idempotent upserts.
"""

import json
import os
import sys

# Ensure project root is on sys.path.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Load .env so MIE_PG_DSN is available without shell export.
_env_path = os.path.join(_ROOT, ".env")
if os.path.exists(_env_path):
    for line in open(_env_path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

dsn = os.environ.get("MIE_PG_DSN")
if not dsn:
    sys.exit("MIE_PG_DSN not set")

# 1. Read ALL existing data from PostgreSQL before closing the connection.
from database.pg_store import PostgresStorage
pg = PostgresStorage(dsn=dsn)
rows, total = pg.list_stations(limit=50)
print(f"Found {total} organizations in database")

enrich_input = []
for row in rows:
    ik = row["identity_key"]
    record = {
        "id": ik,
        "name": row.get("name", ""),
        "website": row.get("website"),
        "country": row.get("country"),
        "state_or_region": row.get("state_or_region"),
        "city": row.get("city"),
        "station_type": row.get("station_type", "unknown"),
        "genres": row.get("genres") or [],
        "formats": row.get("formats") or [],
        "social_urls": row.get("social_urls") or {},
        "source_urls": row.get("source_urls") or [],
        "confidence_score": row.get("confidence_score", 0.0),
        "confidence_reasons": row.get("confidence_reasons") or [],
        "status": row.get("status", "discovered"),
        "description": row.get("description"),
        "language": row.get("language"),
        "market_area": row.get("market_area"),
        "raw_metadata": row.get("raw_metadata") or {},
        "contact_url": None,
        "submission_url": None,
        "programming_url": None,
    }
    record["contacts"] = pg.get_station_contacts(ik)
    record["emails"] = pg.get_station_emails(ik)
    record["phone_numbers"] = pg.get_station_phones(ik)
    enrich_input.append(record)

pg.close()  # all reads done; safe to close

print(f"\nPrepared {len(enrich_input)} records for enrichment:")
for r in enrich_input:
    print(f"  - {r['name']} ({r['id']})")

# 2. Run live enrichment with staff-page extraction.
from discovery.radio.enrich_pipeline import EnrichmentEngine

engine = EnrichmentEngine()
engine.set_live()
print("\nRunning live enrichment (this will fetch pages)...")
result = engine.enrich_records(enrich_input)

print(f"\nEnrichment complete:")
print(f"  Records enriched: {result.record_count}")
print(f"  Failures: {result.failure_count}")
for record_dict in result.records:
    name = record_dict.get("name", "?")
    contacts = record_dict.get("contacts", [])
    emails_found = set()
    named_contacts = []
    for c in contacts:
        if c.get("email"):
            emails_found.add(c["email"])
        if c.get("name"):
            named_contacts.append(c)
    print(f"\n  {name}:")
    print(f"    Contacts: {len(contacts)}")
    print(f"    Emails: {sorted(emails_found) or '(none)'}")
    print(f"    Named persons: {len(named_contacts)}")
    for c in named_contacts:
        print(f"      - {c['name']} ({c.get('role', '?')}) "
              f"email={c.get('email', '-')} "
              f"confidence={c.get('confidence_score', 0):.2f}")

# 3. Persist enriched results back to PostgreSQL (idempotent upsert).
pg2 = PostgresStorage(dsn=dsn)
report = pg2.ingest_intelligence(result.records, source="phase2-live-test")
pg2.close()

print(f"\nIngestion report:")
print(f"  Records accepted: {report.records_accepted}")
print(f"  Records failed: {report.records_failed}")
print(f"  Stations upserted: {report.stations_upserted}")
print(f"  Contacts upserted: {report.contacts_upserted}")
print(f"  Submissions stored: {report.submissions_stored}")
if report.failures:
    for f in report.failures:
        print(f"  FAILURE: {f}")

# 4. List final state.
pg3 = PostgresStorage(dsn=dsn)
final_rows, final_total = pg3.list_stations(limit=50)
print(f"\nFinal state: {final_total} organizations")
for row in final_rows:
    ik = row["identity_key"]
    contacts = pg3.get_station_contacts(ik)
    named = [c for c in contacts if c.get("name")]
    emailed = [c for c in contacts if c.get("email")]
    print(f"\n  {row['name']} ({ik})")
    print(f"    Status: {row.get('status', '?')}")
    print(f"    Confidence: {row.get('confidence_score', 0):.2f}")
    print(f"    Total contacts: {len(contacts)}")
    print(f"    Named persons: {len(named)}")
    for c in named:
        print(f"      * {c['name']} -- role={c.get('role', '?')} "
              f"email={c.get('email', '-')} "
              f"confidence={c.get('confidence_score', 0):.2f}")
    emailed_only = [c for c in emailed if not c.get("name")]
    if emailed_only:
        print(f"    Email-only contacts: {len(emailed_only)}")
        for c in emailed_only:
            print(f"      * {c['email']} -- role={c.get('role', '?')} "
                  f"confidence={c.get('confidence_score', 0):.2f}")
pg3.close()
