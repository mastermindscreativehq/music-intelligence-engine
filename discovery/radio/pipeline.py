"""Radio discovery pipeline: composes the reusable stages end-to-end.

    DiscoveryRequest → queries → provider → candidates
      → URL normalization → bounded retrieval → focused page discovery
      → public contact extraction → normalization → classification
      → deduplication → confidence scoring → StationRecord(s)
      → DiscoveryResult

Every stage failure is recorded (DiscoveryResult.failures) and the run
continues; a single bad site never aborts discovery.

Minimal internal CLI (no production API yet):

    python -m discovery.radio.pipeline --request request.json --seed seeds.json
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from crawler.http import DEFAULT_USER_AGENT, StdlibHttpFetcher, utc_now_iso
from crawler.page_finder import select_priority_pages, score_link
from crawler.pages import parse_html
from crawler.urls import InvalidUrlError, canonical_domain, normalize_url

from discovery import events as ev
from discovery.models import (
    Candidate,
    DiscoveryRequest,
    DiscoveryResult,
    Failure,
    Fact,
    fact_observe_again,
)
from discovery.providers import DiscoveryProvider, SeedListProvider
from discovery.queries import build_queries
from discovery.radio.schema import ContactRecord, StationRecord

from enrichment.confidence import rescore
from enrichment.contacts import build_contacts_from_page
from enrichment.dedupe import deduplicate_stations
from enrichment.emails import email_quality, extract_emails_from_text
from enrichment.stations import classify_station, detect_social_urls

logger = logging.getLogger("mie.discovery.radio")

_TITLE_NOISE = re.compile(
    r"\s*[\|\u2013\u2014-]\s*(home(page)?|official (site|website)|welcome)\s*$",
    re.I,
)


def clean_title(raw: str) -> str:
    title = _TITLE_NOISE.sub("", raw or "").strip()
    return " ".join(title.split())


@dataclass
class EngineConfig:
    timeout_seconds: float = 15.0
    max_pages_per_site: int = 6
    crawl_delay_seconds: float = 1.0
    respect_robots: bool = True
    max_candidates_hard_cap: int = 500


class RadioDiscoveryEngine:
    """Orchestrates the Phase 2 radio discovery stages."""

    def __init__(
        self,
        provider: DiscoveryProvider,
        fetcher=None,
        config: EngineConfig | None = None,
        event_logger: logging.Logger | None = None,
    ) -> None:
        self.provider = provider
        self.fetcher = fetcher or StdlibHttpFetcher(
            timeout_seconds=15.0,
            rate_limit_seconds=1.0,
            respect_robots=True,
            user_agent=DEFAULT_USER_AGENT,
        )
        self.config = config or EngineConfig()
        self.log = event_logger or logger

    # ------------------------------------------------------------------ run

    def run(self, request: DiscoveryRequest) -> DiscoveryResult:
        result = DiscoveryResult(
            request=request.to_dict(), queries=[])
        try:
            queries = build_queries(request)
            result.queries = queries
            ev.log_event(
                self.log, ev.EVENT_DISCOVERY_STARTED,
                query=request.query, limit=request.limit,
                queries=len(queries),
            )
            candidates = self.provider.search(request, queries) or []
            for candidate in candidates[:request.limit]:
                ev.log_event(
                    self.log, ev.EVENT_CANDIDATE_FOUND,
                    url=candidate.url, source=candidate.source,
                )
        except Exception as exc:
            message = f"provider failed: {type(exc).__name__}: {exc}"
            result.failures.append(Failure("provider", "provider_error", message))
            ev.log_event(self.log, ev.EVENT_DISCOVERY_FAILED, reason=message)
            return result

        domain_groups = self._group_candidates(candidates, request, result)

        records: list[dict] = []
        for domain, group in domain_groups.items():
            try:
                record = self._process_site(domain, group, request, result)
                if record is not None:
                    records.append(record)
                    ev.log_event(
                        self.log, ev.EVENT_STATION_NORMALIZED,
                        domain=domain, name=record.get("name"),
                    )
            except Exception as exc:  # one bad site never kills the run
                result.failures.append(Failure(
                    stage="site_processing",
                    error_kind="unexpected",
                    message=f"{type(exc).__name__}: {exc}",
                    url=group[0][0].url,
                ))

        records, duplicates_removed = deduplicate_stations(records)
        if duplicates_removed:
            ev.log_event(self.log, ev.EVENT_DUPLICATE_DETECTED,
                         count=duplicates_removed)
        for record in records:
            rescore(record)

        records.sort(key=lambda r: r.get("confidence_score", 0), reverse=True)
        result.records = records
        ev.log_event(
            self.log, ev.EVENT_DISCOVERY_COMPLETED,
            records=len(records), failures=len(result.failures),
            duplicates_removed=duplicates_removed,
        )
        return result

    # ------------------------------------------------------- candidate prep

    def _group_candidates(
        self,
        candidates: list[Candidate],
        request: DiscoveryRequest,
        result: DiscoveryResult,
    ) -> dict[str, list[tuple[Candidate, str]]]:
        """Group by canonical domain; normalize once per candidate URL."""
        groups: dict[str, list[tuple[Candidate, str]]] = {}
        for candidate in candidates[: min(request.limit,
                                          self.config.max_candidates_hard_cap)]:
            try:
                normalized = normalize_url(candidate.url)
            except (InvalidUrlError, ValueError) as exc:
                result.failures.append(Failure(
                    stage="url_normalization", error_kind="invalid_url",
                    message=str(exc), url=candidate.url,
                ))
                continue
            ev.log_event(self.log, ev.EVENT_URL_NORMALIZED,
                         url=candidate.url, normalized=normalized)
            key = canonical_domain(normalized)
            groups.setdefault(key, []).append((candidate, normalized))
        return dict(list(groups.items())[: request.limit])

    # ------------------------------------------------------------ site work

    def _process_site(
        self,
        domain: str,
        group: list[tuple[Candidate, str]],
        request: DiscoveryRequest,
        result: DiscoveryResult,
    ) -> dict | None:
        first_candidate, homepage_url = group[0]
        record = StationRecord()
        record.id = str(uuid.uuid4())

        source_urls: list[str] = []
        discovered_times: list[str] = []
        titles: list[str] = []
        snippets: list[str] = []
        for candidate, normalized in group:
            if normalized not in source_urls:
                source_urls.append(normalized)
            discovered_times.append(candidate.discovered_at)
            if candidate.title:
                titles.append(candidate.title)
            if candidate.snippet:
                snippets.append(candidate.snippet)

        record.website = homepage_url
        record.source_urls = source_urls
        record.discovered_at = min(discovered_times) if discovered_times \
            else utc_now_iso()
        record.last_observed_at = utc_now_iso()

        station_domain = canonical_domain(homepage_url)

        home_result = self.fetcher.fetch(homepage_url)
        ev.log_event(
            self.log, ev.EVENT_PAGE_FETCHED,
            url=homepage_url, ok=home_result.ok,
            error_kind=home_result.error_kind,
        )
        if not home_result.ok:
            result.failures.append(Failure(
                stage="homepage_fetch",
                error_kind=home_result.error_kind or "unknown_error",
                message=home_result.error_message or "homepage fetch failed",
                url=homepage_url,
            ))
            record.name = clean_title(titles[0]) if titles else station_domain
            record.raw_metadata["candidate_snippet"] = snippets[0][:280] if snippets else ""
            return record.to_dict()   # broken-site path; scorer penalizes

        record.website_reachable = True
        home_page = parse_html(home_result.final_url or homepage_url,
                               home_result.body or "")
        homepage_title = clean_title(home_page.title)

        # --- focused page discovery --------------------------------------
        budget = max(0, self.config.max_pages_per_site - 1)
        priority_urls = select_priority_pages(
            home_result.final_url or homepage_url, home_page, budget)

        pages = [home_page]
        page_meta: dict[str, tuple[str | None, int | None]] = {
            (home_result.final_url or homepage_url):
                (home_result.content_type, home_result.status)
        }
        for url in priority_urls:
            page_result = self.fetcher.fetch(url)
            ev.log_event(
                self.log, ev.EVENT_PAGE_FETCHED,
                url=url, ok=page_result.ok,
                error_kind=page_result.error_kind,
            )
            if not page_result.ok:
                result.failures.append(Failure(
                    stage="priority_page_fetch",
                    error_kind=page_result.error_kind or "unknown_error",
                    message=page_result.error_message or "fetch failed",
                    url=url,
                ))
                continue
            parsed = parse_html(page_result.final_url or url,
                                page_result.body or "")
            pages.append(parsed)
            page_meta[parsed.url] = (page_result.content_type,
                                     page_result.status)

        # --- name resolution ---------------------------------------------
        candidate_name = clean_title(titles[0]) if titles else ""
        record.name = homepage_title or candidate_name or domain
        record.name_matches_site = self._names_match(
            record.name, homepage_title, candidate_name)

        # --- emails / phones / socials ------------------------------------
        site_domains = {station_domain}
        emails_by_value: dict[str, dict] = {}
        phones_by_value: dict[str, dict] = {}
        all_links: list[str] = []

        for page in pages:
            all_links.extend(link.href_absolute for link in page.links)
            source_type = self._source_type_for(page.url)
            for email in extract_emails_from_text(page.text or ""):
                quality = email_quality(email, site_domains)
                existing = emails_by_value.get(email)
                fact = {
                    "value": email,
                    "source_url": page.url,
                    "source_type": source_type,
                    "method": "text_rule",
                    "discovered_at": utc_now_iso(),
                    "also_seen_at": [],
                    "quality": quality,
                }
                if existing is None:
                    emails_by_value[email] = fact
                    ev.log_event(self.log, ev.EVENT_EMAIL_EXTRACTED,
                                 email=email, source_url=page.url)
                else:
                    fact_observe_again(existing, page.url)
            for phone_fact in self._phone_facts(page):
                value = phone_fact["value"]
                if value not in phones_by_value:
                    phones_by_value[value] = phone_fact

        record.emails = list(emails_by_value.values())
        record.phone_numbers = list(phones_by_value.values())
        record.social_urls = detect_social_urls(all_links)

        # --- contacts ------------------------------------------------------
        contacts_by_email: dict[str, ContactRecord] = {}
        unnamed_contacts: list[ContactRecord] = []
        for page in pages:
            for raw in build_contacts_from_page(page):
                contact = ContactRecord.from_dict(raw)
                contact.station_id = record.id
                for prov in contact.provenance:
                    prov.setdefault("discovered_at", utc_now_iso())
                if contact.email and contact.email in contacts_by_email:
                    existing = contacts_by_email[contact.email]
                    existing.provenance.extend(contact.provenance)
                    if existing.name is None and contact.name:
                        existing.name = contact.name
                elif contact.email:
                    contacts_by_email[contact.email] = contact
                else:
                    unnamed_contacts.append(contact)
        record.contacts = list(contacts_by_email.values()) + unnamed_contacts

        # --- page role URLs --------------------------------------------------
        best_contact = self._best_marked_page(pages, "contact")
        best_submission = self._best_marked_page(pages, "submission")
        best_programming = self._best_marked_page(pages, "programming")
        now_iso = utc_now_iso()
        if best_contact:
            record.contact_url = Fact(best_contact, best_contact,
                                      "contact_page", discovered_at=now_iso) \
                .to_dict()
        if best_submission:
            record.submission_url = Fact(best_submission, best_submission,
                                         "submission_page",
                                         discovered_at=now_iso).to_dict()
        if best_programming:
            record.programming_url = Fact(
                best_programming, best_programming, "programming_page",
                discovered_at=now_iso).to_dict()

        # --- classification ---------------------------------------------------
        texts = [page.text or "" for page in pages] + snippets
        classification = classify_station(texts)
        record.station_type = classification.station_type
        record.classification_confidence = classification.confidence
        record.classification_evidence = classification.evidence
        ev.log_event(
            self.log, ev.EVENT_STATION_CLASSIFIED,
            domain=domain, station_type=classification.station_type,
            confidence=round(classification.confidence, 2),
        )

        record.raw_metadata["homepage_title"] = homepage_title
        record.raw_metadata["pages_fetched"] = [p.url for p in pages]
        return record.to_dict()

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _names_match(*candidates: str | None) -> bool:
        """Token-overlap check between candidate names and site title."""
        values = [v.lower() for v in candidates if v]
        if len(values) < 2:
            return False
        base_tokens = set(re.findall(r"[a-z0-9]+", values[0]))
        base_tokens -= {"radio", "the", "and", "of", "fm", "am"}
        for other in values[1:]:
            other_tokens = set(re.findall(r"[a-z0-9]+", other))
            other_tokens -= {"radio", "the", "and", "of", "fm", "am",
                             "home", "homepage", "welcome", "official"}
            if base_tokens & other_tokens:
                return True
        return False

    @staticmethod
    def _source_type_for(url: str) -> str:
        lowered = url.lower()
        if re.search(r"submi", lowered):
            return "submission_page"
        if re.search(r"contact", lowered):
            return "contact_page"
        return "official_website_page"

    def _phone_facts(self, page) -> list[dict]:
        from enrichment.contacts import extract_phone_numbers
        facts: list[dict] = []
        for phone in extract_phone_numbers(page.text or ""):
            facts.append({
                "value": phone,
                "source_url": page.url,
                "source_type": "official_website_page",
                "method": "phone_rule",
                "discovered_at": utc_now_iso(),
                "also_seen_at": [],
            })
        return facts

    @staticmethod
    def _best_marked_page(pages, marker: str) -> str | None:
        """Highest keyword-weight page whose URL matches the marker."""
        pattern = {
            "contact": re.compile(r"contact", re.I),
            "submission": re.compile(r"submi", re.I),
            "programming": re.compile(r"program|shows|schedule", re.I),
        }[marker]
        best_url: str | None = None
        best_weight = 0
        for page in pages:
            path_only = urlsplit(page.url).path
            if not pattern.search(path_only):
                continue
            weight = score_link(path_only, "")
            if weight > best_weight:
                best_weight = weight
                best_url = page.url
        return best_url


# --------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="discovery.radio.pipeline",
        description=(
            "Run the Phase 2 radio discovery engine against a local seed "
            "file. Prints one JSON document."
        ),
    )
    parser.add_argument("--request", required=True,
                        help="path to a discovery request JSON file")
    parser.add_argument("--seed", help="path to a seed JSON file")
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args(argv)

    try:
        with open(args.request, encoding="utf-8") as handle:
            request_data = json.load(handle)
        request = DiscoveryRequest.from_dict(request_data)
        if not args.seed:
            raise SystemExit("--seed is required in Phase 2 (no credential "
                             "search providers are configured)")
        provider = SeedListProvider(args.seed)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2

    engine = RadioDiscoveryEngine(provider)
    result = engine.run(request)
    print(json.dumps(result.to_dict(), indent=args.indent, ensure_ascii=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry
    sys.exit(main())
