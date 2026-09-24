"""Enrichment engine and CLI for radio intelligence (Phase 3).

Takes DiscoveryResult-shaped JSON (or a bare list of StationRecord dicts)
and produces enriched RadioIntelligenceRecords.

Network behavior — deliberate and explicit:

- DEFAULT is OFFLINE: only facts already present in the input are
  re-assembled; no requests are made. This keeps the phase deterministic,
  testable, and safe.
- Live fetching is opt-in via ``--fetch`` (or an injected fetcher). Even
  then, fetching is bounded: robots.txt respected, per-station URL budget,
   per-host rate limiting (all inherited from crawler.http).
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

from crawler.http import StdlibHttpFetcher
from crawler.page_finder import score_link, select_priority_pages
from crawler.pages import ParsedPage, parse_html
from crawler.urls import canonical_domain, normalize_url

from discovery.events import (
    EVENT_ENRICHMENT_COMPLETED,
    EVENT_ENRICHMENT_FAILED,
    EVENT_ENRICHMENT_PAGE_FETCH,
    EVENT_ENRICHMENT_STARTED,
    EVENT_STATION_ENRICHED,
    EVENT_SUBMISSION_PATH_FOUND,
    get_logger,
    log_event,
)

from discovery.models import EnrichmentResult, Failure, utc_now_iso
from discovery.radio.intelligence import (
    build_intelligence_record,
    merge_contacts_from_pages,
    rebuild_contact_channels,
)
from discovery.radio.readiness import compute_outreach_readiness
from discovery.radio.schema import SourceFetchRecord
from enrichment.confidence import score_contact


class EngineConfig:
    """Knobs for the enrichment engine (mirrors pipeline.EngineConfig)."""

    def __init__(
        self,
        max_pages_per_station: int = 6,
        verify_pages_per_station: int = 8,
        person_pages_per_station: int = 6,
        timeout_seconds: float = 15.0,
        rate_limit_seconds: float = 1.0,
        respect_robots: bool = True,
        user_agent: str = "MIE-EnrichmentBot/0.1 (+respectful; stdlib)",
        logger=None,
    ) -> None:
        self.max_pages_per_station = max(0, int(max_pages_per_station))
        # Separate, additional budget used to *verify* already-discovered
        # useful pages (reachability evidence), distinct from the discovery
        # fetch budget above. Verification only ever requests exact URLs that
        # were already discovered as links on crawled pages — it never invents
        # routes.
        self.verify_pages_per_station = max(
            0, int(verify_pages_per_station))
        # Dedicated budget for the contact-person discovery pass. It fetches
        # EXACT already-discovered, still-unverified person-relevant URLs
        # (about / staff / programming pages the verification budget may not
        # have reached) and only ever parses what the station itself
        # publishes. Never guesses a route; never fabricates a person/email.
        self.person_pages_per_station = max(
            0, int(person_pages_per_station))
        self.timeout_seconds = timeout_seconds
        self.rate_limit_seconds = rate_limit_seconds
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self.logger = logger or get_logger("mie.enrichment")


# Categories worth verifying first (the outreach-relevant pages). "other"
# clutter is never fetched purely to become actionable.
_VERIFY_CATEGORY_ORDER = {
    "send_music": 0,
    "submission_guidelines": 1,
    "dj_directory": 2,
    "contact": 3,
    "programming": 4,
    "about": 5,
    "other": 6,
}

# Useful-page categories whose fetched bodies may surface named people
# (music/programming directors, DJs/hosts, station staff). Bodies of these
# categories are parsed into person pages so contact evidence is merged in
# without any extra fetch — verification budget is reused.
_PERSON_PARSE_CATEGORIES = ("dj_directory", "contact", "programming")

# Categories targeted by the dedicated person-discovery pass. Any still-
# unverified person-relevant page that the verification budget did not reach
# is a candidate; only exact discovered URLs are ever fetched.
_PERSON_DISCOVER_CATEGORIES = _PERSON_PARSE_CATEGORIES + (
    "about", "send_music", "submission_guidelines")

# Order in which person-relevant categories are prioritized for the dedicated
# person pass (most people-relevant first).
_PERSON_DISCOVER_ORDER = {
    "dj_directory": 0,
    "contact": 1,
    "programming": 2,
    "about": 3,
    "send_music": 4,
    "submission_guidelines": 5,
}


class _UsefulPageVerifier:
    """Bounded reachability verification of already-discovered useful pages.

    Only EXACT URLs that were discovered as links on crawled pages are ever
    fetched — this class never constructs or guesses a route. It records a
    ``SourceFetchRecord`` per verification request and writes the outcome
    (reachable / status / rechecked_at) back onto the matching
    ``UsefulPage``. Pages already carrying verified reachability evidence
    from an earlier fetch are skipped.
    """

    def __init__(self, fetcher, logger) -> None:
        self._fetcher = fetcher
        self._logger = logger

    def verify(self, enriched, budget: int) -> tuple[list[SourceFetchRecord], list[ParsedPage]]:
        if budget <= 0 or not enriched.useful_pages:
            return [], []
        already_fetched = _exact_ok_urls(enriched.fetches or [])
        # Order candidates: highest-value category first, then stable by URL.
        candidates = [
            p for p in enriched.useful_pages
            if p.reachable is not True      # already verified/success: skip
            and p.url not in already_fetched  # exact URL already fetched: skip
        ]
        candidates.sort(key=lambda p: (
            _VERIFY_CATEGORY_ORDER.get(p.category, 6), p.url))
        to_check = candidates[:budget]
        new_records: list[SourceFetchRecord] = []
        person_pages: list[ParsedPage] = []
        for page in to_check:
            url = page.url
            fetched_at = utc_now_iso()
            try:
                fetch = self._fetcher.fetch(url)
            except Exception as exc:
                fetch = None
                rec = SourceFetchRecord(
                    url=url, ok=False,
                    error_kind=type(exc).__name__, fetched_at=fetched_at)
                page.reachable = False
                page.status = None
                page.rechecked_at = fetched_at
                new_records.append(rec)
                log_event(self._logger, EVENT_ENRICHMENT_PAGE_FETCH,
                          url=url, ok=False, status=None)
                continue
            ok = bool(fetch.ok)
            rec = SourceFetchRecord(
                url=url, ok=ok, status=getattr(fetch, "status", None),
                error_kind=getattr(fetch, "error_kind", None),
                fetched_at=fetched_at)
            page.reachable = ok
            page.status = getattr(fetch, "status", None)
            page.rechecked_at = fetched_at
            new_records.append(rec)
            log_event(self._logger, EVENT_ENRICHMENT_PAGE_FETCH,
                      url=url, ok=ok, status=getattr(fetch, "status", None))
            # Parse a reachable person-relevant page body (reusing the same
            # verification fetch — no extra request) so named directors,
            # DJs/hosts and staff contacts can be merged into the record.
            if ok and getattr(fetch, "body", None) \
                    and page.category in _PERSON_PARSE_CATEGORIES:
                content_type = (getattr(fetch, "content_type", "") or "").lower()
                if "html" in content_type or not content_type:
                    person_pages.append(parse_html(url, fetch.body))
        return new_records, person_pages


def _exact_ok_urls(fetches) -> set[str]:
    ok: set[str] = set()
    for f in fetches:
        try:
            if f.ok and f.url:
                ok.add(f.url.rstrip("/"))
        except AttributeError:
            continue
    return ok


class EnrichmentEngine:
    """Orchestrates enrichment of discovered station records."""

    def __init__(
        self,
        config: EngineConfig | None = None,
        fetcher=None,
        role_advisor=None,
    ) -> None:
        self.config = config or EngineConfig()
        # Optional Phase 5 AI hook: callable(context_text) ->
        # (role, metadata | None). Default None keeps enrichment fully
        # deterministic and offline; see enrichment.llm.suggest_contact_role.
        self._role_advisor = role_advisor
        if fetcher is not None:
            self._fetcher = fetcher          # injectable (tests / offline)
            self._owns_fetcher = False
        else:
            self._fetcher = None             # offline unless set_live()
            self._owns_fetcher = False

    # -- lifecycle ----------------------------------------------------------

    def set_live(self) -> None:
        """Opt in to network fetching with conservative defaults."""
        if self._fetcher is None:
            cfg = self.config
            self._fetcher = StdlibHttpFetcher(
                timeout_seconds=cfg.timeout_seconds,
                rate_limit_seconds=cfg.rate_limit_seconds,
                respect_robots=cfg.respect_robots,
                user_agent=cfg.user_agent,
            )
            self._owns_fetcher = True

    @property
    def live(self) -> bool:
        return self._fetcher is not None

    # -- main entry -----------------------------------------------------------

    def enrich_records(self, records: list[dict]) -> EnrichmentResult:
        result = EnrichmentResult()
        log_event(self.config.logger, EVENT_ENRICHMENT_STARTED,
                  station_count=len(records), mode="live" if self.live else "offline")
        for index, record in enumerate(records):
            try:
                enriched = self._enrich_one(record)
                result.records.append(enriched.to_dict())
                submission = enriched.submission
                log_event(
                    self.config.logger, EVENT_STATION_ENRICHED,
                    station=enriched.name,
                    genres=enriched.genres[:5],
                    contact_count=len(enriched.contacts),
                    submission_found=bool(submission),
                    confidence_score=enriched.confidence_score,
                )
                if submission:
                    log_event(
                        self.config.logger, EVENT_SUBMISSION_PATH_FOUND,
                        station=enriched.name,
                        methods=(submission.methods or {}).get("methods", []),
                        confidence_score=submission.confidence_score,
                    )
            except Exception as exc:  # one bad station never kills the run
                try:
                    website = record.get("website")
                except Exception:
                    website = None
                failure = Failure(
                    stage="enrichment",
                    error_kind=type(exc).__name__,
                    message=str(exc),
                    url=website if isinstance(website, str) else None,
                )
                result.failures.append(failure)
                log_event(self.config.logger, EVENT_ENRICHMENT_FAILED,
                          target=failure.url or "unknown",
                          reason=f"{failure.error_kind}: {failure.message}")
        result.completed_at = utc_now_iso()
        log_event(self.config.logger, EVENT_ENRICHMENT_COMPLETED,
                  record_count=result.record_count,
                  failure_count=result.failure_count)
        return result

    # -- internals ---------------------------------------------------------------

    def _enrich_one(self, record: dict):
        pages: list[ParsedPage] = []
        fetch_records: list[SourceFetchRecord] = []

        targets = self._collect_fetch_targets(record)
        budget = self.config.max_pages_per_station

        if self.live and targets:
            initial_targets = targets[:budget]
            pages, fetch_records = self._fetch_pages(initial_targets)
            fetched_count = len(initial_targets)

            # Discover high-value internal links from the pages we just read.
            extra_urls = self._discover_internal_pages(
                pages, targets, budget - fetched_count)
            if extra_urls:
                extra_pages, extra_records = self._fetch_pages(extra_urls)
                pages.extend(extra_pages)
                fetch_records.extend(extra_records)

        enriched = build_intelligence_record(record, pages, fetch_records)
        person_pages: list[ParsedPage] = []
        if self.live:
            # Phase 6: bounded link-verification pass. Confirm which discovered
            # useful pages are truly reachable by fetching their EXACT URLs
            # (never guessing routes), writing reachable/status/rechecked_at
            # evidence so only verified links surface as actionable.
            verifier = _UsefulPageVerifier(self._fetcher, self.config.logger)
            new_records, verified_person_pages = verifier.verify(
                enriched,
                budget=getattr(self.config, "verify_pages_per_station", 8))
            if new_records:
                fetch_records.extend(new_records)
                enriched.fetches = list(fetch_records)
                # The verifier wrote reachability evidence onto each UsefulPage
                # AFTER build_intelligence_record serialized them into
                # raw_metadata. Re-serialize so the persisted/API list carries
                # the same reachable/status/rechecked_at evidence.
                enriched.raw_metadata["useful_pages"] = [
                    p.to_dict() for p in enriched.useful_pages
                ]
            person_pages.extend(verified_person_pages)
            # Phase 1 person-discovery pass: reach person-relevant useful
            # pages the verification budget did not cover (about / staff /
            # programming / submissions pages), still fetching only EXACT
            # already-discovered URLs and honoring the per-station budget.
            discover_records, discover_pages = self._discover_person_pages(
                enriched,
                budget=getattr(self.config, "person_pages_per_station", 6))
            if discover_records:
                fetch_records.extend(discover_records)
                enriched.fetches = list(fetch_records)
                enriched.raw_metadata["useful_pages"] = [
                    p.to_dict() for p in enriched.useful_pages
                ]
            person_pages.extend(discover_pages)
            # Phase 1 outreach-readiness: merge named people (directors,
            # DJs/hosts, station staff) from verified person-relevant pages —
            # reusing the same verification fetches, no extra request — then
            # recompute the contact-channels bundle so newly surfaced director
            # identities/emails feed the readiness projection.
            if person_pages:
                merge_contacts_from_pages(enriched, person_pages)
                rebuild_contact_channels(enriched)
        # Contact-person layer: conservative named-person candidates from ALL
        # parsed pages (build + verify + person pass). Stored verbatim so the
        # readiness projection can report the station's own published person
        # and evidence; an unverified page/name/email is never invented here.
        # (person.py is excluded from this deployment; readiness falls back to
        # the evidence-backed director channels below.)
        if self._role_advisor is not None:
            self._apply_role_advisor(enriched)
        # Outreach readiness is projected for every enriched record (offline and
        # live): it keys off qualification, fetch/reachability evidence, and the
        # evidence-backed contact/submission channels — never inventing a
        # person, email, or URL.
        enriched.raw_metadata["outreach_readiness"] = \
            compute_outreach_readiness(enriched.to_dict())
        return enriched

    def _apply_role_advisor(self, enriched) -> None:
        """Opt-in Phase 5 hook: AI-hint roles ONLY for unknown contacts.

        Deterministic rules already ran inside build_intelligence_record;
        the advisor is consulted strictly as a fallback. A validated hint
        flips the role and appends an inference provenance entry carrying
        method/model/prompt version (docs/ai-architecture.md). Anything
        ambiguous stays "unknown" — the honest default.
        """
        site_domains = {enriched.domain} if enriched.domain else set()
        for contact in enriched.contacts or []:
            if contact.role != "unknown":
                continue
            context = "\n".join(
                part for part in (contact.name, contact.email,
                                  contact.phone, contact.source_url)
                if part)
            if not context.strip():
                continue
            try:
                role, meta = self._role_advisor(context)
            except Exception:
                continue  # advisor failure never kills enrichment
            if role == "unknown" or not isinstance(meta, dict):
                continue
            contact.role = role
            contact.provenance.append({
                "kind": "inference",
                "method": str(meta.get("method") or "llm"),
                "model": meta.get("model"),
                "prompt_version": meta.get("prompt_version"),
                "value": f"role:{role}",
                "observed_at": utc_now_iso(),
            })
            score, reasons = score_contact(contact.to_dict(), site_domains)
            contact.confidence_score = score
            contact.confidence_reasons = reasons + [
                "role inferred by local model (see provenance)"]

    def _collect_fetch_targets(self, record: dict) -> list[str]:
        """Known URLs worth re-reading, priority ordered, deduplicated.

        Priority: dedicated pages first (submission > programming > contact),
        then website root, then previously seen source URLs.
        """
        targets: list[str] = []
        seen: set[str] = set()

        def add(url) -> None:
            if isinstance(url, str) and url.startswith(("http://", "https://")) \
                    and url not in seen:
                seen.add(url)
                targets.append(url)

        for key in ("submission_url", "programming_url", "contact_url"):
            fact = record.get(key)
            if isinstance(fact, dict):
                add(fact.get("value"))
        add(record.get("website"))
        for url in record.get("source_urls") or []:
            add(url)
        return targets

    def _discover_internal_pages(
        self,
        fetched_pages: list[ParsedPage],
        already_fetched: list[str],
        remaining_budget: int,
    ) -> list[str]:
        """Discover high-value internal links from pages we just read.

        Uses ``crawler.page_finder.select_priority_pages`` for the first
        (homepage) page to get keyword-ranked links *and* conventional
        fallback paths.  Additional pages are scored with
        ``score_link``.  Only same-site links are considered;
        already-fetched URLs are excluded.  Returns at most
        *remaining_budget* URLs, best-scored first.
        """
        if remaining_budget <= 0 or not fetched_pages:
            return []

        seen: set[str] = set(already_fetched)
        result: list[str] = []

        # --- Phase 1: use select_priority_pages for the first page
        # (typically the homepage) to get keyword-ranked same-site links.
        # Guessed conventional fallback paths (GUESSED_PATHS) are excluded
        # because the enrichment engine already fetches dedicated URLs
        # (submission_url, contact_url, etc.) in _collect_fetch_targets.
        first_page = fetched_pages[0]
        try:
            first_site = canonical_domain(first_page.url)
        except ValueError:
            first_site = None
        if first_site is not None:
            try:
                hp_normalized = normalize_url(first_page.url)
                seen.add(hp_normalized)
            except ValueError:
                pass
            # Paths actually present as links on the page; used to
            # filter out guessed fallback paths from select_priority_pages.
            link_paths: set[str] = set()
            for link in first_page.links:
                link_paths.add(urlparse(link.href_absolute).path.rstrip("/"))
            ranked = select_priority_pages(
                first_page.url, first_page, remaining_budget)
            for url in ranked:
                if url in seen:
                    continue
                # Only keep pages whose path was an actual link on the
                # page; skip guessed conventional fallback paths.
                url_path = urlparse(url).path.rstrip("/")
                if url_path not in link_paths:
                    continue
                seen.add(url)
                result.append(url)
            remaining_budget -= len(result)

        if remaining_budget <= 0:
            return result

        # --- Phase 2: score links from remaining pages (if any) and
        # any first-page links not already selected.
        sites: set[str] = set()
        for page in fetched_pages:
            try:
                sites.add(canonical_domain(page.url))
            except ValueError:
                pass
        if not sites:
            return result

        scored: list[tuple[int, int, str]] = []
        order = 0
        for page in fetched_pages:
            for link in page.links:
                url = link.href_absolute
                if not url.lower().startswith(("http://", "https://")):
                    continue
                try:
                    if canonical_domain(url) not in sites:
                        continue
                    normalized = normalize_url(url)
                except ValueError:
                    continue
                if normalized in seen:
                    continue
                weight = score_link(normalized, link.anchor_text)
                if weight <= 0:
                    continue
                seen.add(normalized)
                scored.append((-weight, order, normalized))
                order += 1

        scored.sort()
        return result + [url for _, _, url in scored[:remaining_budget]]

    def _fetch_pages(self, urls: list[str]):
        pages: list[ParsedPage] = []
        records: list[SourceFetchRecord] = []
        for url in urls:
            fetched_at = utc_now_iso()
            try:
                fetch = self._fetcher.fetch(url)
            except Exception as exc:
                records.append(SourceFetchRecord(
                    url=url, ok=False,
                    error_kind=type(exc).__name__,
                    fetched_at=fetched_at))
                continue
            ok = bool(fetch.ok)
            records.append(SourceFetchRecord(
                url=url,
                ok=ok,
                status=getattr(fetch, "status", None),
                error_kind=getattr(fetch, "error_kind", None),
                fetched_at=fetched_at,
            ))
            log_event(self.config.logger, EVENT_ENRICHMENT_PAGE_FETCH,
                      url=url, ok=ok,
                      status=getattr(fetch, "status", None))
            if ok and getattr(fetch, "body", None):
                content_type = (getattr(fetch, "content_type", "") or "").lower()
                if "html" in content_type or not content_type:
                    pages.append(parse_html(url, fetch.body))
        return pages, records

    def _discover_person_pages(
        self,
        enriched,
        budget: int,
    ) -> tuple[list[SourceFetchRecord], list[ParsedPage]]:
        """Dedicated contact-person discovery pass over discovered pages.

        Fetches person-relevant useful pages (staff/DJ directories, contact,
        programming, about, submissions) that the verification budget did not
        reach, in priority order, bounded by ``person_pages_per_station``.
        Only EXACT URLs already discovered as links on crawled pages are ever
        fetched — no routes are guessed, robots/rate limits are respected. For
        each page it records a ``SourceFetchRecord``, writes reachability
        evidence back onto the ``UsefulPage``, and parses a reachable body so
        named people (and only published, role-associated ones) can surface.
        """
        if budget <= 0 or not getattr(enriched, "useful_pages", None):
            return [], []
        already_fetched = _exact_ok_urls(enriched.fetches or [])
        candidates = [
            p for p in enriched.useful_pages
            if p.category in _PERSON_DISCOVER_CATEGORIES
            and p.reachable is not True      # already verified/success: skip
            and p.url not in already_fetched  # exact URL already fetched: skip
        ]
        candidates.sort(key=lambda p: (
            _PERSON_DISCOVER_ORDER.get(p.category, 9), p.url))
        to_check = candidates[:budget]
        records: list[SourceFetchRecord] = []
        person_pages: list[ParsedPage] = []
        by_url = {p.url.rstrip("/"): p for p in candidates}
        for url in [p.url for p in to_check]:
            fetched_at = utc_now_iso()
            try:
                fetch = self._fetcher.fetch(url)
            except Exception as exc:
                rec = SourceFetchRecord(
                    url=url, ok=False,
                    error_kind=type(exc).__name__, fetched_at=fetched_at)
                records.append(rec)
                page = by_url.get(url.rstrip("/"))
                if page is not None:
                    page.reachable = False
                    page.rechecked_at = fetched_at
                continue
            ok = bool(fetch.ok)
            rec = SourceFetchRecord(
                url=url, ok=ok, status=getattr(fetch, "status", None),
                error_kind=getattr(fetch, "error_kind", None),
                fetched_at=fetched_at)
            records.append(rec)
            log_event(self.config.logger, EVENT_ENRICHMENT_PAGE_FETCH,
                      url=url, ok=ok, status=getattr(fetch, "status", None))
            page = by_url.get(url.rstrip("/"))
            if page is not None:
                page.reachable = ok
                page.status = getattr(fetch, "status", None)
                page.rechecked_at = fetched_at
            if ok and getattr(fetch, "body", None):
                content_type = (getattr(fetch, "content_type", "") or "").lower()
                if "html" in content_type or not content_type:
                    person_pages.append(parse_html(url, fetch.body))
        return records, person_pages


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_records(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = None
        for key in ("records", "stations"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                records = candidate
                break
        if records is None:
            raise ValueError(
                "input must be a JSON array of records or a DiscoveryResult "
                "object with a 'records' or 'stations' array")
    else:
        raise ValueError(
            "input must be a JSON array of records or a DiscoveryResult "
            "object with a 'records' or 'stations' array")
    out: list[dict] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        if "website" not in r and "url" in r:
            r["website"] = r["url"]
        out.append(r)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m discovery.radio.enrich",
        description=(
            "Enrich discovered radio-station records into intelligence "
            "records. Offline by default; pass --fetch to read pages."))
    parser.add_argument("--input", required=True,
                        help="path to discovery output JSON")
    parser.add_argument("--output", default="-",
                        help="output path (default stdout)")
    parser.add_argument("--fetch", action="store_true",
                        help="enable bounded live fetching (default: offline)")
    parser.add_argument("--ai-roles", action="store_true",
                        help="opt-in: consult the local Ollama model for "
                             "unknown contact roles (falls back to "
                             "'unknown' when unavailable; no network "
                             "without it)")
    args = parser.parse_args(argv)

    records = _load_records(args.input)
    engine = EnrichmentEngine()
    if args.ai_roles:
        from functools import partial
        from enrichment.llm import (OllamaClient, OllamaConfig,
                                    suggest_contact_role)
        engine._role_advisor = partial(
            suggest_contact_role,
            client=OllamaClient(OllamaConfig.from_env()))
    if args.fetch:
        engine.set_live()
    result = engine.enrich_records(records)
    payload = json.dumps(result.to_dict(), indent=2, ensure_ascii=False)
    if args.output == "-":
        sys.stdout.write(payload + "\n")
    else:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
