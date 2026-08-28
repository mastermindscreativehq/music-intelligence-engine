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
from discovery.radio.intelligence import build_intelligence_record
from discovery.radio.schema import SourceFetchRecord
from enrichment.confidence import score_contact


class EngineConfig:
    """Knobs for the enrichment engine (mirrors pipeline.EngineConfig)."""

    def __init__(
        self,
        max_pages_per_station: int = 6,
        timeout_seconds: float = 15.0,
        rate_limit_seconds: float = 1.0,
        respect_robots: bool = True,
        user_agent: str = "MIE-EnrichmentBot/0.1 (+respectful; stdlib)",
        logger=None,
    ) -> None:
        self.max_pages_per_station = max(0, int(max_pages_per_station))
        self.timeout_seconds = timeout_seconds
        self.rate_limit_seconds = rate_limit_seconds
        self.respect_robots = respect_robots
        self.user_agent = user_agent
        self.logger = logger or get_logger("mie.enrichment")


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
        if self._role_advisor is not None:
            self._apply_role_advisor(enriched)
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
