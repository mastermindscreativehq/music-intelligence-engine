"""DJ-flavoured discovery providers, fully independent of the radio pipeline.

The radio ``SeedListProvider`` reads the ``stations`` key of a seed file, so
feeding it a DJ seed file (which uses the ``djs`` key) yields zero candidates.
That mismatch silently starves the DJ discovery engine. This module provides a
DJ counterpart that reads the ``djs`` key and emits the same ``Candidate``
contract the DJ engine already consumes — keeping provenance (``source`` =
``djs_seed:<file>``, geography carried through) with no fabrication.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from discovery.models import (
    Candidate,
    DiscoveryRequest,
    SourceType,
)


def _clean_title(name: Any, url: Any) -> str:
    title = " ".join(str(name or "").split())
    return title or str(url)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _matches_geography(entry: dict, request: DiscoveryRequest) -> bool:
    for field in ("country", "state_or_region", "city"):
        wanted = getattr(request, field)
        if not wanted:
            continue
        have = str(entry.get(field) or "").strip().lower()
        if have and have != str(wanted).strip().lower():
            return False
    return True


class DjsSeedListProvider:
    """Deterministic provider over a local DJ seed file (``djs`` key)."""

    def __init__(self, seed_path: str | Path) -> None:
        self.seed_path = Path(seed_path)
        self._entries = self._load()

    @property
    def _seed_kind(self) -> str:
        return f"djs_seed:{self.seed_path.name}"

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.seed_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"djs seed file not found: {self.seed_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"djs seed file is not valid JSON: {exc}") from exc
        if isinstance(raw, dict):
            entries = raw.get("djs", [])
        elif isinstance(raw, list):
            entries = raw
        else:
            raise ValueError(
                "djs seed file must be an object with 'djs' or a list")
        cleaned: list[dict] = []
        for entry in entries:
            if isinstance(entry, dict) and (
                    str(entry.get("website") or "").strip()
                    or str(entry.get("url") or "").strip()):
                cleaned.append(entry)
        return cleaned

    def search(
        self, request: DiscoveryRequest, queries: Sequence[str],
    ) -> list[Candidate]:
        out: list[Candidate] = []
        for entry in self._entries:
            if len(out) >= request.limit:
                break
            if not _matches_geography(entry, request):
                continue
            url = str(entry.get("website") or entry.get("url") or "").strip()
            if not url:
                continue
            out.append(Candidate(
                title=_clean_title(entry.get("name") or entry.get("title"),
                                   url),
                url=url,
                source=self._seed_kind,
                snippet=" ".join(str(entry.get("snippet") or "")).strip()
                if False else _clean(str(entry.get("snippet") or "")) or "",
                source_type=SourceType.DIRECTORY_SOURCE,
                country=_clean(entry.get("country")),
                state_or_region=_clean(entry.get("state_or_region")),
                city=_clean(entry.get("city")),
            ))
        return out

    def load_entries(self) -> list[dict]:
        """Return the raw seed entries so the caller can keep provenance."""
        return [dict(e) for e in self._entries]
