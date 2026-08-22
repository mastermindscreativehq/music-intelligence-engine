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

from crawler.http import StdlibHttpFetcher
from crawler.pages import ParsedPage, parse_html

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
    ) -> None:
        self.config = config or EngineConfig()
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

        targets = self._collect_fetch_targets(record)[:self.config.max_pages_per_station]
        if self.live and targets:
            pages, fetch_records = self._fetch_pages(targets)

        return build_intelligence_record(record, pages, fetch_records)

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
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        records = data.get("records")
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)]
    raise ValueError(
        "input must be a JSON array of records or a DiscoveryResult object "
        "with a 'records' array")


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
    args = parser.parse_args(argv)

    records = _load_records(args.input)
    engine = EnrichmentEngine()
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
