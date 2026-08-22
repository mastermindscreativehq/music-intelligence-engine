"""Discovery provider abstraction.

A provider turns (request, queries) into Candidate URLs. The engine is not
coupled to any search vendor.

Implemented now:
- SeedListProvider: reads candidate station URLs/names from a local JSON
  seed file (produced from legitimate public sources). Honest and
  credential-free.

Documented extension point (NOT implemented — requires credentials):
- HttpSearchProvider against a real search API behind the same Protocol.
  Nothing in this repo fakes live search results.

Expected seed file shape (either form):

    {"stations": [{"name": "...", "url": "...",
                   "country": "...", "state_or_region": "..."}]}
or
    [{"name": "...", "url": "..."}, ...]
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol, Sequence

from discovery.models import Candidate, DiscoveryRequest, SourceType


class DiscoveryProvider(Protocol):
    """Anything with ``search(request, queries) -> list[Candidate]``."""

    def search(
        self, request: DiscoveryRequest, queries: Sequence[str]
    ) -> list[Candidate]:  # pragma: no cover - protocol
        ...


def _clean_title(name: str, url: str) -> str:
    title = " ".join((name or "").split())
    return title or url


class SeedListProvider:
    """Deterministic provider over a local JSON seed file."""

    def __init__(self, seed_path: str | Path) -> None:
        self.seed_path = Path(seed_path)
        self._entries = self._load()

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.seed_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"seed file not found: {self.seed_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"seed file is not valid JSON: {exc}") from exc
        if isinstance(raw, dict):
            entries = raw.get("stations", [])
        elif isinstance(raw, list):
            entries = raw
        else:
            raise ValueError("seed file must be an object with 'stations' or a list")
        cleaned: list[dict] = []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str) \
                    and entry["url"].strip():
                cleaned.append(entry)
        return cleaned

    def search(
        self, request: DiscoveryRequest, queries: Sequence[str]
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        for entry in self._entries:
            if not self._matches_geography(entry, request):
                continue
            url = entry["url"].strip()
            candidates.append(Candidate(
                title=_clean_title(str(entry.get("name") or ""), url),
                url=url,
                source=f"seed_file:{self.seed_path.name}",
                snippet=" ".join(str(entry.get("snippet") or "").split()),
                source_type=SourceType.DIRECTORY_SOURCE,
            ))
            if len(candidates) >= request.limit:
                break
        return candidates

    @staticmethod
    def _matches_geography(entry: dict, request: DiscoveryRequest) -> bool:
        for field_name in ("country", "state_or_region"):
            wanted = getattr(request, field_name)
            have = str(entry.get(field_name) or "").strip().lower()
            if wanted and have and have != wanted.strip().lower():
                return False
        return True


class StaticListProvider:
    """In-memory provider (tests / programmatic use)."""

    def __init__(self, entries: Sequence[dict]) -> None:
        self.entries = [dict(e) for e in entries]

    def search(
        self, request: DiscoveryRequest, queries: Sequence[str]
    ) -> list[Candidate]:
        out: list[Candidate] = []
        for entry in self.entries:
            if len(out) >= request.limit:
                break
            if not SeedListProvider._matches_geography(entry, request):
                continue
            out.append(Candidate(
                title=_clean_title(str(entry.get("name") or ""),
                                   str(entry.get("url") or "")),
                url=str(entry["url"]),
                source=str(entry.get("source") or "static_list"),
                snippet=str(entry.get("snippet") or ""),
                source_type=SourceType.DIRECTORY_SOURCE,
            ))
        return out
