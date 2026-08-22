"""Structured event logging for discovery operations.

Events are single-line JSON via the stdlib logging module — greppable and
parseable without dependencies. Secrets never appear in events (none are
handled by this layer anyway).
"""

from __future__ import annotations

import json
import logging

EVENT_DISCOVERY_STARTED = "discovery_started"
EVENT_CANDIDATE_FOUND = "candidate_found"
EVENT_URL_NORMALIZED = "url_normalized"
EVENT_PAGE_FETCHED = "page_fetched"
EVENT_CONTACT_PAGE_FOUND = "contact_page_found"
EVENT_EMAIL_EXTRACTED = "email_extracted"
EVENT_STATION_CLASSIFIED = "station_classified"
EVENT_DUPLICATE_DETECTED = "duplicate_detected"
EVENT_STATION_NORMALIZED = "station_normalized"
EVENT_DISCOVERY_COMPLETED = "discovery_completed"
EVENT_DISCOVERY_FAILED = "discovery_failed"

# Phase 3 enrichment events
EVENT_ENRICHMENT_STARTED = "enrichment_started"
EVENT_STATION_ENRICHED = "station_enriched"
EVENT_ENRICHMENT_PAGE_FETCH = "enrichment_page_fetch"
EVENT_SUBMISSION_PATH_FOUND = "submission_path_found"
EVENT_ENRICHMENT_COMPLETED = "enrichment_completed"
EVENT_ENRICHMENT_FAILED = "enrichment_failed"


def get_logger(name: str = "mie.discovery") -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    """Emit one structured event as a single JSON log line."""
    payload = {"event": event}
    payload.update(fields)
    logger.info(json.dumps(payload, default=str, ensure_ascii=True))
