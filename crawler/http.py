"""Bounded, polite HTTP retrieval.

Design rules (docs/radio-discovery.md §6):
- http/https only; no authentication, CAPTCHA, or anti-bot interaction ever
- configurable timeout and maximum response size
- content-type gate (HTML/plain text only)
- robots.txt respected via urllib.robotparser with a per-host cache
- per-host rate limiting between requests
- every failure is classified so the pipeline can record it and continue

The fetcher is an implicit protocol: anything exposing ``fetch(url) ->
FetchResult`` can replace it (tests use a fixture-backed fake).
"""

from __future__ import annotations

import logging
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_USER_AGENT = (
    "MusicIntelligenceEngine/0.2 (+public station discovery; "
    "contact: operator-configured)"
)

logger = logging.getLogger("mie.crawler.http")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class FetchResult:
    """Outcome of a single fetch attempt. Exactly one of body/error set."""

    url: str
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    body: str | None = None
    error_kind: str | None = None   # timeout|dns_error|http_status|invalid_url|
                                    # content_type|too_large|connection_error|
                                    # ssl_error|robots_disallowed|unexpected
    error_message: str | None = None
    fetched_at: str = field(default_factory=utc_now_iso)

    @property
    def ok(self) -> bool:
        return self.error_kind is None and self.body is not None


class RateLimiter:
    """Enforce a minimum delay between requests to the same host."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = max(0.0, float(min_interval_seconds))
        self._last_request: dict[str, float] = {}

    def wait(self, host_key: str) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        last = self._last_request.get(host_key)
        if last is not None:
            remaining = self.min_interval - (now - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host_key] = time.monotonic()


class RobotsCache:
    """Per-host robots.txt cache built on urllib.robotparser."""

    def __init__(self, user_agent: str, timeout: float,
                 enabled: bool = True) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.enabled = enabled
        self._cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    def allows(self, url: str) -> bool:
        if not self.enabled:
            return True
        parts = urllib.request.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        parser = self._cache.get(base)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{base}/robots.txt")
            try:
                parser.read()
            except Exception as exc:  # unreachable robots -> fail open
                logger.info("robots_unreadable host=%s error=%s", base, exc)
                self._cache[base] = parser
                return True
            self._cache[base] = parser
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True


class StdlibHttpFetcher:
    """Conservative stdlib-based fetcher implementing fetch(url)->FetchResult."""

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        max_bytes: int = 2_000_000,
        rate_limit_seconds: float = 1.0,
        respect_robots: bool = True,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.timeout = timeout_seconds
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.rate_limiter = RateLimiter(rate_limit_seconds)
        self.robots = RobotsCache(user_agent, timeout_seconds, respect_robots)

    def fetch(self, url: str) -> FetchResult:
        result = FetchResult(url=url)
        try:
            normalized_scheme_ok = url.lower().startswith(("http://", "https://"))
        except Exception:
            normalized_scheme_ok = False
        if not normalized_scheme_ok:
            result.error_kind = "invalid_url"
            result.error_message = "only http/https URLs are fetched"
            return result
        if not self.robots.allows(url):
            result.error_kind = "robots_disallowed"
            result.error_message = "disallowed by robots.txt"
            logger.info("page_fetch event=page_fetched status=robots_disallowed")
            return result

        host_key = urllib.request.urlsplit(url).netloc.lower()
        self.rate_limiter.wait(host_key)

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                result.status = int(getattr(resp, "status", 0) or 0)
                result.final_url = resp.geturl()
                raw_type = resp.headers.get("Content-Type", "") if resp.headers else ""
                result.content_type = raw_type.split(";", 1)[0].strip().lower()
                payload = resp.read(self.max_bytes + 1)
        except urllib.error.HTTPError as exc:
            result.status = int(exc.code)
            result.error_kind = "http_status"
            result.error_message = f"HTTP {exc.code}"
            return result
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            text = str(reason).lower()
            if isinstance(reason, socket.timeout) or "timed out" in text:
                result.error_kind = "timeout"
            elif "getaddrinfo" in text or "name or service" in text or "nodename" in text:
                result.error_kind = "dns_error"
            elif "certificate" in text or "ssl" in text:
                result.error_kind = "ssl_error"
            else:
                result.error_kind = "connection_error"
            result.error_message = str(reason)
            return result
        except socket.timeout as exc:
            result.error_kind = "timeout"
            result.error_message = str(exc)
            return result
        except Exception as exc:  # never crash a run on one page
            result.error_kind = "unexpected"
            result.error_message = f"{type(exc).__name__}: {exc}"
            return result

        if len(payload) > self.max_bytes:
            result.error_kind = "too_large"
            result.error_message = f"body exceeds {self.max_bytes} bytes"
            return result
        if result.content_type not in ("text/html", "application/xhtml+xml", "text/plain"):
            result.error_kind = "content_type"
            result.error_message = f"unsupported content type {result.content_type!r}"
            return result

        charset = "utf-8"
        try:
            body_text = payload.decode(charset, errors="replace")
        except Exception:
            body_text = payload.decode("latin-1", errors="replace")
        result.body = body_text
        logger.info(
            "page_fetched url=%s status=%s bytes=%d",
            result.final_url or url, result.status, len(payload),
        )
        return result
