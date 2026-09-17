"""DJ discovery pipeline: independent DJs, fully separated from radio.

DJ discovery is deliberately aligned with (but independent of) the radio
pipeline: the same reusable discovery models, providers, fetcher, and
liberal-safe extraction rules are composed for *DJ professionals* — club /
event / party / touring / festival / wedding DJs and collectives.

Separation rules:

- A DJ record NEVER links to or derives from a radio station; ``station_key``
  / ``station_name`` are optional metadata only, and the radio pipeline never
  creates DJ records.
- Every extracted fact keeps its provenance: channels and source_urls always
  carry the public page where the value was observed.
- Absent evidence stays absent: if a DJ's site cannot be fetched, the record
  is kept (from the public seed/candidate) with ZERO fabricated channels.

Composition:

    DiscoveryRequest→queries→provider→candidates
      → home page fetch → name/geo carry-through
      → email / social / contact-route extraction (provenance kept)
      → strong-identity dedupe (djs.dedupe) → DJ payloads → DiscoveryResult

CLI (no production API yet):

    python -m discovery.djs.pipeline --seed seeds.json [--db db.sqlite]
    python -m discovery.djs.pipeline --request request.json \
        --seed candidates.json [--db db.sqlite]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from typing import Any
from urllib.parse import urlsplit

from crawler.http import DEFAULT_USER_AGENT, FetchResult, StdlibHttpFetcher
from crawler.pages import parse_html
from crawler.urls import InvalidUrlError, canonical_domain, normalize_url

from discovery import events as ev
from discovery.models import (
    Candidate,
    DiscoveryRequest,
    DiscoveryResult,
    Failure,
    utc_now_iso,
)
from discovery.djs.providers import DjsSeedListProvider
from discovery.providers import DiscoveryProvider

from djs.dedupe import deduplicate_djs
from djs.service import ingest_dj_discovery

from enrichment.emails import email_quality, extract_emails_from_text
from enrichment.stations import detect_social_urls

logger = logging.getLogger("mie.discovery.djs")

_TITLE_NOISE = re.compile(
    r"\s*[\|\u2013\u2014-]\s*(home(page)?|official (site|website)|welcome)"
    r"|^\s*(dj|dee?jay)\s+\|\s*",
    re.I,
)

_MAX_QUERIES = 8


def clean_dj_title(raw: str) -> str:
    """Collapse whitespace and strip trailing '| Official Site' noise."""
    title = _TITLE_NOISE.sub("", raw or "").strip()
    return " ".join(title.split())


def build_dj_queries(request: DiscoveryRequest) -> list[str]:
    """Deterministic DJ discovery queries from a structured request.

    The request ``query`` is the subject head (e.g. ``DJ``, ``Afrobeats DJ``);
    ``genre`` and the geography fields extend it. Pure function: same input,
    same queries. No hard-coded cities.
    """
    head_parts = [request.query, request.genre]
    subject = " ".join(part for part in head_parts if part).strip() or "DJ"

    geo_parts = [request.city, request.state_or_region, request.country]
    geo = " ".join(part for part in geo_parts if part).strip()

    patterns = [
        "{subject} {geo}".strip(),
        "{subject} club {geo}".strip(),
        "club {subject} {geo}".strip(),
        "independent {subject} {geo}".strip(),
        "party {subject} {geo}".strip(),
        "{subject} events {geo}".strip(),
        "{subject} bookings {geo}".strip(),
        "{subject} nightlife {geo}".strip(),
    ]
    queries: list[str] = []
    for pattern in patterns:
        query = " ".join(pattern.format(subject=subject, geo=geo).split())
        if query and query not in queries:
            queries.append(query)
        if len(queries) >= _MAX_QUERIES:
            break
    return queries


def _first_geo(candidate: Candidate, request: DiscoveryRequest,
               field: str) -> str | None:
    value = getattr(candidate, field)
    if value:
        return " ".join(str(value).split())
    fallback = getattr(request, field)
    if fallback:
        return " ".join(str(fallback).split())
    return None


class DjsDiscoveryEngine:
    """Orchestrates source-backed DJ discovery over provider candidates.

    Follows the radio engine's fault model: one bad candidate yields a
    ``Failure`` and the run continues.
    """

    def __init__(self, provider: DiscoveryProvider, fetcher=None,
                 config: dict[str, Any] | None = None) -> None:
        self.provider = provider
        self.fetcher = fetcher or StdlibHttpFetcher(
            timeout_seconds=15.0,
            rate_limit_seconds=1.0,
            respect_robots=True,
            user_agent=DEFAULT_USER_AGENT,
        )
        self.config = config or {}

    def run(self, request: DiscoveryRequest) -> DiscoveryResult:
        result = DiscoveryResult(request=request.to_dict(), queries=[])
        try:
            queries = build_dj_queries(request)
            result.queries = queries
            ev.log_event(logger, ev.EVENT_DISCOVERY_STARTED,
                         query=request.query, limit=request.limit,
                         queries=len(queries))
            candidates = self.provider.search(request, queries) or []
        except Exception as exc:  # provider-level failure never crashes
            message = f"provider failed: {type(exc).__name__}: {exc}"
            result.failures.append(Failure("provider", "provider_error", message))
            ev.log_event(logger, ev.EVENT_DISCOVERY_FAILED, reason=message)
            return result

        records: list[dict] = []
        for candidate in candidates[: request.limit]:
            try:
                record = self._process_candidate(candidate, request, result)
                if record is not None:
                    records.append(record)
                    ev.log_event(
                        logger, "dj_candidate_discovered",
                        url=candidate.url, name=record.get("name"))
            except Exception as exc:  # one bad candidate never kills the run
                result.failures.append(Failure(
                    stage="candidate_processing",
                    error_kind="unexpected",
                    message=f"{type(exc).__name__}: {exc}",
                    url=candidate.url,
                ))

        records, duplicates_removed = deduplicate_djs(records)
        result.records = records
        ev.log_event(logger, ev.EVENT_DISCOVERY_COMPLETED,
                     records=len(records), failures=len(result.failures),
                     duplicates_removed=duplicates_removed)
        return result

    def _process_candidate(self, candidate: Candidate, request: DiscoveryRequest,
                           result: DiscoveryResult) -> dict | None:
        from discovery.djs.qualify import (
            VERDICT_NEEDS_REVIEW,
            VERDICT_REJECTED,
            classify_candidate,
        )

        try:
            homepage_url = normalize_url(candidate.url)
        except (InvalidUrlError, ValueError) as exc:
            result.failures.append(Failure(
                stage="url_normalization", error_kind="invalid_url",
                message=str(exc), url=candidate.url))
            return None

        # Qualification gate (pre-fetch): a candidate on an event/ticketing/
        # playlist/listing page is rejected up front; its evidence URL is
        # never even crawled.
        classification = classify_candidate(
            url=homepage_url, title=candidate.title or "",
            snippet=candidate.snippet or "")
        if classification.verdict == VERDICT_REJECTED:
            result.failures.append(Failure(
                stage="dj_qualification", error_kind="not_a_dj",
                message=classification.reason, url=homepage_url))
            return None

        record = {
            "name": clean_dj_title(candidate.title) or homepage_url,
            "role": None,
            "country": _first_geo(candidate, request, "country"),
            "state_or_region": _first_geo(candidate, request, "state_or_region"),
            "city": _first_geo(candidate, request, "city"),
            "genres": [],
            "formats": [],
            "source_urls": [homepage_url],
            "channels": [],
            "verification": {
                "discovered": True,
                "discovered_from": candidate.source,
                "candidate_snippet": (candidate.snippet or "")[:280],
            },
            "discovered_at": candidate.discovered_at or utc_now_iso(),
            "last_observed_at": utc_now_iso(),
        }

        fetch = self.fetcher.fetch(homepage_url)
        ev.log_event(logger, ev.EVENT_PAGE_FETCHED,
                     url=homepage_url, ok=fetch.ok,
                     error_kind=fetch.error_kind)
        if not fetch.ok:
            # No fabrication: a DJ whose site can't be fetched keeps ONLY the
            # facts observed on the public seed/candidate page — no channels.
            result.failures.append(Failure(
                stage="homepage_fetch",
                error_kind=fetch.error_kind or "unknown_error",
                message=fetch.error_message or "homepage fetch failed",
                url=homepage_url,
            ))
            # A page that could not be fetched cannot raise the candidate to
            # qualified evidence; needs_review candidates are not ingested.
            if classification.verdict == VERDICT_NEEDS_REVIEW:
                result.failures.append(Failure(
                    stage="dj_qualification",
                    error_kind="needs_human_review",
                    message=classification.reason, url=homepage_url))
                return None
            record["verification"]["page_fetch"] = "failed"
            record["verification"]["classification"] = \
                classification.to_dict()
            return record

        page = parse_html(fetch.final_url or homepage_url, fetch.body or "")
        page_title = clean_dj_title(page.title)
        if page_title:
            record["name"] = page_title
        final = fetch.final_url or homepage_url
        if final not in record["source_urls"]:
            record["source_urls"].append(final)

        # Qualification gate (post-fetch): the fetched page title is the
        # ultimate evidence; downgraded verdicts (rejected / needs_review)
        # are never ingested.
        classification = classify_candidate(
            url=homepage_url, title=candidate.title or "",
            snippet=candidate.snippet or "", page_title=page.title)
        if classification.verdict == VERDICT_REJECTED:
            result.failures.append(Failure(
                stage="dj_qualification", error_kind="not_a_dj",
                message=classification.reason, url=homepage_url))
            return None
        if classification.verdict == VERDICT_NEEDS_REVIEW:
            result.failures.append(Failure(
                stage="dj_qualification", error_kind="needs_human_review",
                message=classification.reason, url=homepage_url))
            return None

        domain = canonical_domain(homepage_url)
        all_links = [link.href_absolute for link in page.links
                     if link.href_absolute]
        channels = self._channel_facts(page, fetch, all_links, domain)
        record["channels"] = channels
        record["verification"]["page_fetch"] = "ok"
        record["verification"]["site_title"] = page_title or record["name"]
        record["verification"]["classification"] = classification.to_dict()
        return record

    def _channel_facts(self, page, fetch: FetchResult,
                       all_links: list[str], domain: str) -> list[dict]:
        """Extract source-backed channel facts observed on one page."""
        channels: list[dict] = []
        page_url = page.url or (fetch.final_url or "")
        site_domains = {domain}

        socials = detect_social_urls(all_links)
        for platform, url in socials.items():
            channels.append({
                "channel": platform,
                "value": url,
                "source_url": page_url,
            })

        role = _page_role(page_url)
        for email in extract_emails_from_text(page.text or ""):
            email_quality(email, site_domains)  # quality kept implicit
            channels.append({
                "channel": "submission_email" if role == "submission_page"
                else "email",
                "value": email,
                "source_url": page_url,
            })

        booking_url = _best_routed_link(all_links, "booking")
        if booking_url:
            channels.append({
                "channel": "contact_page",
                "value": booking_url,
                "source_url": booking_url,
            })
        return _unique_channels(channels)


def _unique_channels(channels: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in channels:
        key = (str(entry.get("channel") or ""),
               str(entry.get("value") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


_BOOKING_MARKERS = re.compile(
    r"book(ings?|ing|ed)?|gig|booking|hire[-_]?a[-_]?dj|press[-_]?kit", re.I)


def _page_role(url: str) -> str | None:
    path = urlsplit(url).path.lower()
    if re.search(r"submi", path):
        return "submission_page"
    if re.search(r"contact", path):
        return "contact_page"
    return None


def _best_routed_link(links: list[str], marker: str) -> str | None:
    """First same-domain link whose path matches the marker, else None."""
    for url in links:
        try:
            if _BOOKING_MARKERS.search(urlsplit(url).path):
                return url
        except ValueError:
            continue
    return None


# ------------------------------------------------------------------ seeds/CLI

def load_dj_seed_entries(seed_path: str) -> list[dict]:
    """Load source-backed DJ seed entries from a JSON file.

    Accepts a list or ``{"djs": [...]}``. Every entry is normalized into a
    DJ create/discovery payload: the top-level ``website`` becomes a
    ``website`` channel, ``source_url`` joins ``source_urls``. Provenance is
    enforced at ingestion (channel facts require an http(s) ``source_url``).
    """
    import pathlib
    try:
        raw = json.loads(pathlib.Path(seed_path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"seed file not found: {seed_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"seed file is not valid JSON: {exc}") from exc
    if isinstance(raw, dict):
        entries = raw.get("djs", [])
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("seed file must be an object with 'djs' or a list")

    payloads: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each DJ seed entry must be an object")
        payload = {key: entry.get(key) for key in (
            "name", "stage_name", "role", "program", "station_key",
            "station_name", "platform", "country", "state_or_region",
            "city", "genres", "formats", "verification")}
        source_url = str(entry.get("source_url") or "").strip()
        if source_url and not (source_url.startswith("http://")
                               or source_url.startswith("https://")):
            raise ValueError(f"source_url must be http(s): {source_url!r}")
        channels = [dict(c) for c in entry.get("channels") or []]
        website = str(entry.get("website") or "").strip()
        if website:
            if not (website.startswith("http://")
                    or website.startswith("https://")):
                raise ValueError(f"website must be http(s): {website!r}")
            if not any(c.get("channel") == "website" and c.get("value") == website
                       for c in channels):
                channels.append({
                    "channel": "website", "value": website,
                    "source_url": source_url or website})
        source_urls = [str(u) for u in entry.get("source_urls") or []
                       if str(u).strip()]
        if source_url and source_url not in source_urls:
            source_urls = [source_url, *source_urls]
        payload["channels"] = channels
        payload["source_urls"] = source_urls
        payloads.append(payload)
    return payloads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="discovery.djs.pipeline",
        description=(
            "Discover or ingest independent DJ records. Without --request "
            "the rich seed file is ingested as-is; with --request the "
            "candidate crawl path runs first."
        ),
    )
    parser.add_argument("--seed", required=True,
                        help="path to a DJ seed JSON file")
    parser.add_argument("--request",
                        help="path to a discovery request JSON file (crawl)")
    parser.add_argument("--db", help="SQLite DB to ingest into (otherwise "
                                     "prints discovered payloads as JSON)")
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)

    try:
        from database.service import PersistenceService
        entries = load_dj_seed_entries(args.seed)
        discovered: list[dict] = entries
        if args.request:
            with open(args.request, encoding="utf-8") as handle:
                request = DiscoveryRequest.from_dict(json.load(handle))
            provider = DjsSeedListProvider(args.seed)
            engine = DjsDiscoveryEngine(provider)
            result = engine.run(request)
            discovered = result.records
        if not args.db:
            print(json.dumps(
                {"discovered": discovered,
                 "count": len(discovered)},
                indent=args.indent, ensure_ascii=True))
            return 0
        service = PersistenceService(args.db)
        try:
            report = ingest_dj_discovery(service, discovered)
        finally:
            service.close()
        print(json.dumps({
            "created": report["created"],
            "merged": report["merged"],
            "duplicates_removed": report["duplicates_removed"],
            "failures": report["failures"],
            "stored": [r.get("name") for r in report["records"]],
        }, indent=args.indent, ensure_ascii=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":  # pragma: no cover - manual invocation entry
    sys.exit(main())