"""Content-addressed storage for music submission assets (Phase 8).

The contract-first rule (approved boundary correction #1): only OPAQUE
asset keys cross this boundary. The database, API envelopes, and any
future consumer never see filesystem paths — they see
``track_id = 'sha256:<hex>'``. Swapping this class for a Supabase/S3
implementation therefore cannot change the submission contract.

Blobs are immutable: identical bytes deduplicate to one stored object.
Writes are atomic (temp file + rename) so a crashed upload can never
leave a partial blob behind.
"""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORAGE_ROOT = REPO_ROOT / "data" / "submissions" / "tracks"


def track_id_for(sha256_hex: str) -> str:
    """Opaque public identifier for a content hash."""
    return f"sha256:{sha256_hex}"


class TrackStore(ABC):
    """Minimal seam for asset storage; keys are opaque sha256 identifiers."""

    @abstractmethod
    def put(self, data: bytes) -> str:
        """Store bytes (dedup); returns the opaque track_id."""

    @abstractmethod
    def read(self, key: str) -> bytes:
        """Return the bytes for *key*; raises KeyError when absent."""

    @abstractmethod
    def contains(self, key: str) -> bool:
        """True when *key* is already stored."""


class LocalTrackStore(TrackStore):
    """Filesystem implementation; layout is an internal detail."""

    def __init__(self, root=None) -> None:
        self._root = Path(root) if root is not None else DEFAULT_STORAGE_ROOT

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        key = track_id_for(digest)
        if self.contains(key):
            return key
        target = self._blob_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)          # atomic on POSIX and Windows
        return key

    def read(self, key: str) -> bytes:
        digest = _require_hash_key(key)
        try:
            return self._blob_path(digest).read_bytes()
        except OSError as exc:
            raise KeyError(key) from exc

    def contains(self, key: str) -> bool:
        digest = _require_hash_key(key)
        return self._blob_path(digest).is_file()

    # -- internal ------------------------------------------------------------

    def _blob_path(self, digest: str) -> Path:
        # Layout detail — NEVER exposed through the repository/API surface.
        return self._root / "blobs" / digest[:2] / f"{digest}.mp3"


def _require_hash_key(key: str) -> str:
    prefix = "sha256:"
    if not isinstance(key, str) or not key.startswith(prefix):
        raise KeyError(key)
    digest = key[len(prefix):]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise KeyError(key)
    return digest
