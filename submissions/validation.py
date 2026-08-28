"""Pure upload validation for music submission assets (Phase 8).

No IO here — functions take bytes/strings and return verdicts, so every
rule is unit-testable offline. Storage and API adapters apply these
verdicts; nothing in this module knows about databases or HTTP.

Policy (minimal, contract-first):

- MP3 only (roadmap Phase 8 wording): an upload is accepted when it is
  either ID3v2-tagged (leading ``ID3`` magic) or begins with a valid
  MPEG audio frame sync. Declared Content-Type headers are advisory —
  the bytes decide.
- A hard size ceiling is enforced by the caller before storage writes;
  ``validate_upload`` reports oversize as a rejection reason.
- The original filename is display metadata ONLY: sanitized to a safe
  single path component, never influencing where bytes live (blobs are
  content-addressed by SHA-256).
"""

from __future__ import annotations

import re

DEFAULT_MAX_BYTES = 20 * 1024 * 1024      # 20 MB (approved decision D2)
MAX_FILENAME_LENGTH = 200
DEFAULT_FILENAME = "upload.mp3"

# C0 control characters + DEL, built from codepoints so this source file
# itself stays free of raw control bytes.
_CTRL_CLASS = "[" + re.escape(
    "".join(map(chr, list(range(0x00, 0x20)) + [0x7F]))) + "]"
_CONTROL_CHARS = re.compile(_CTRL_CLASS)


def looks_like_mp3(data):
    """True when *data* plausibly decodes as an MP3 stream.

    Accepted shapes:
    - ID3v2 tagged: leading ``ID3`` magic (tag contents are never parsed).
    - Bare MPEG audio frame: 11 sync bits, non-reserved version and layer
      bits, and a non-reserved bitrate index where applicable.
    """
    if len(data) < 4:
        return False
    if data[:3] == b"ID3":
        return True
    b1, b2, b3 = data[0], data[1], data[2]
    if b1 != 0xFF or (b2 & 0xE0) != 0xE0:
        return False
    version_bits = (b2 >> 3) & 0x03     # 00=MPEG2.5 01=reserved 10=V2 11=V1
    layer_bits = (b2 >> 1) & 0x03       # 00=reserved 01=III 10=II 11=I
    if version_bits == 0x01 or layer_bits == 0x00:
        return False
    bitrate_index = (b3 >> 4) & 0x0F
    if bitrate_index == 0x00 or bitrate_index == 0x0F:
        return False                    # 'free' or 'bad' per MPEG tables
    return True


def sanitize_filename(name):
    """Reduce an upload filename to a safe display string.

    Strips any directory components (both separators), control characters,
    and leading dots; collapses whitespace; caps length. Never empty.
    """
    if not name:
        return DEFAULT_FILENAME
    cleaned = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _CONTROL_CHARS.sub("", cleaned).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return DEFAULT_FILENAME
    if len(cleaned) > MAX_FILENAME_LENGTH:
        suffix = ""
        if "." in cleaned:
            candidate_suffix = cleaned.rsplit(".", 1)[1]
            if 0 < len(candidate_suffix) <= 10:
                suffix = "." + candidate_suffix
        cleaned = cleaned[:MAX_FILENAME_LENGTH - len(suffix)] + suffix
    return cleaned


def validate_upload(data, max_bytes=DEFAULT_MAX_BYTES):
    """Verdict for one upload payload.

    Returns ``{"accepted": bool, "reason": str|None}``. Reasons are stable
    operator-facing strings (also persisted as quarantine reject_reason).
    """
    if not data:
        return {"accepted": False, "reason": "empty upload"}
    if len(data) > max_bytes:
        return {"accepted": False,
                "reason": f"payload exceeds {max_bytes} bytes"}
    if not looks_like_mp3(data):
        return {"accepted": False,
                "reason": "not an MP3 stream (missing ID3 tag or "
                          "MPEG frame sync)"}
    return {"accepted": True, "reason": None}
