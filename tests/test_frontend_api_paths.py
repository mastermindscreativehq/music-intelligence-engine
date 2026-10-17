"""Frontend API-path resolution regression (static, offline).

Pins the root cause of the production "malformed envelope from API" / 404
failure on release editing: ``GET /api/v1/tracks/{id}`` was being fetched
against the STATIC frontend origin (Vercel) instead of the API origin
(Railway), because the detail helper treated any path beginning with "/" as
already-absolute and skipped the configured API base.

``links.self`` is ALWAYS a backend-relative path ("/api/v1/tracks/sha256:...").
The helper must join it to API_BASE_URL, and only use a value verbatim when it
is a fully-qualified http(s) URL.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

API_JS = Path(__file__).resolve().parents[1] / "frontend" / "js" / "api.js"


def _track_detail_body() -> str:
    text = API_JS.read_text(encoding="utf-8")
    match = re.search(r"trackDetail:\s*\(selfPath\)\s*=>", text)
    assert match, "trackDetail helper not found in api.js"
    # capture up to the next top-level method key ("name: (" on its own line)
    rest = text[match.end():]
    end = re.search(r"\n\s{2}[A-Za-z]+:", rest)
    return rest[:end.start()] if end else rest


class TestTrackDetailResolution(unittest.TestCase):
    def setUp(self):
        self.body = _track_detail_body()

    def test_joins_backend_relative_paths_to_the_api_base(self):
        self.assertIn("API_BASE_URL", self.body)

    def test_absolute_detection_requires_a_full_url(self):
        # The absolute check is the http(s) regex, not a bare leading slash.
        self.assertIn(r"/^https?:\/\//.test(selfPath)", self.body)

    def test_leading_slash_is_not_treated_as_absolute(self):
        # The old bug: `|| selfPath.startsWith("/")` returned a relative path
        # verbatim (fetched against Vercel). It must not reappear.
        self.assertNotIn('|| selfPath.startsWith("/")', self.body)
        # The join normalizes a leading slash against the base.
        self.assertIn('selfPath.startsWith("/") ? selfPath : "/" + selfPath',
                      self.body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
