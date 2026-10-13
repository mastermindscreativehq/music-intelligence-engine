"""Song-story-aware outreach email composition (optional AI, always honest).

The writer turns the operator's OWN inputs into a recipient-specific email:

- the release's saved song story / creative brief (source material, never a
  verbatim dump and never invented),
- the release metadata already stored (title, genre where known),
- the recipient's verified name / role / organization and any context the
  operator supplied,
- the release's saved official listening URL (included verbatim, never
  fabricated),
- the operator's optional objective, style, and personalization notes.

Two paths, one contract:

- AI path — the local Ollama model (``enrichment.llm``) is consulted with a
  versioned prompt template. Its JSON output is validated against the
  template's declared schema before use; malformed output is rejected and
  the deterministic draft is returned instead.
- Deterministic fallback — always available, fully offline. It composes a
  factual, plainly-worded draft from the same inputs and never claims to
  know a song's meaning when no story was provided.

Either way the result is a DRAFT: ``compose_email`` never creates an outreach
record and never sends anything. Sending stays a separate, explicit operator
action.
"""

from __future__ import annotations

from enrichment.llm import OllamaClient, TemplateError, load_template

PROMPT_DOMAIN = "outreach"
PROMPT_NAME = "intro_email"

# Stable style vocabulary. The label is what an operator would read; the key
# is what the API accepts. Kept small and explicit so unknown styles fail
# loudly rather than silently defaulting.
STYLES: dict[str, str] = {
    "professional": "Professional and direct",
    "warm": "Warm and personal",
    "story": "Story-driven and emotional",
    "concise": "Concise radio submission",
    "industry": "Music-industry introduction",
    "pitch": "Curator or playlist pitch",
}
DEFAULT_STYLE = "professional"

__all__ = ["DEFAULT_STYLE", "STYLES", "compose_email", "default_client"]


def default_client() -> OllamaClient:
    """Process-default Ollama client (env-configured; ``MIE_OLLAMA_*``).

    The AI is OPTIONAL: when the host is unreachable ``compose_email`` falls
    back to the deterministic draft, so the endpoint works with no Ollama
    running. Endpoints use this; tests inject a client/transport instead.
    """
    return OllamaClient()


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _song_title(track: dict) -> str:
    """Human-readable title derived from the stored filename — never invented.

    The filename is the only title the system actually has; the extension is
    stripped and separators tidied. An absent/opaque filename yields "".
    """
    name = _clean(track.get("original_filename"))
    if not name:
        return ""
    for dot in (".mp3", ".wav", ".flac", ".aiff", ".aif", ".m4a", ".ogg"):
        if name.lower().endswith(dot):
            name = name[: -len(dot)]
            break
    return " ".join(name.replace("_", " ").replace("-", " ").split()).strip()


def _resolve_style(payload: dict) -> str:
    raw = payload.get("style")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_STYLE
    style = str(raw).strip().lower()
    if style not in STYLES:
        raise ValueError(
            "'style' must be one of: " + ", ".join(sorted(STYLES)))
    return style


def _resolve_story(track: dict, payload: dict) -> str:
    """The song story to use: an explicit override wins over the saved one."""
    if "song_story" in payload and payload["song_story"] is not None:
        explicit = payload["song_story"]
        if isinstance(explicit, str):
            return explicit.strip()
    return _clean(track.get("song_story"))


def _ensure_link(message: str, url: str) -> str:
    """Guarantee the intended listening URL appears exactly once in the body."""
    if not url or url in message:
        return message
    return message.rstrip() + f"\n\nYou can listen here: {url}"


def _deterministic_draft(*, title: str, artist: str, recipient: str,
                         role: str, organization: str, story: str,
                         url: str, objective: str) -> tuple[str, str]:
    """A factual, offline draft assembled from the operator's own inputs.

    No meaning is asserted that the operator did not provide: without a
    story the body simply does not describe the song.
    """
    who = f' "{title}"' if title else " a new release"
    if recipient:
        greeting = f"Hi {recipient},"
    else:
        greeting = "Hello,"

    if artist:
        opening = (f"I'm {artist}, and I wanted to share{who} with you.")
    else:
        opening = f"I wanted to share{who} with you."

    target = ""
    if organization and role:
        target = f" I'm reaching out because you handle {role.replace('_', ' ')} at {organization}."
    elif organization:
        target = f" I'm reaching out because of your work at {organization}."
    elif role:
        target = (f" I'm reaching out because of your work as "
                  f"{role.replace('_', ' ')}.")

    paragraphs = [greeting, opening + target]
    if story:
        paragraphs.append(story)
    if objective:
        paragraphs.append(objective)
    if url:
        paragraphs.append(f"You can listen here: {url}")
    else:
        paragraphs.append("I'd be glad to send more details or a private "
                          "link if that's useful.")

    signoff = f"\nBest,\n{artist}" if artist else "\nBest,"
    return ("", "\n\n".join(paragraphs) + signoff)


def compose_email(*, track: dict, payload: dict | None = None,
                  client: OllamaClient | None = None) -> dict:
    """Compose one recipient-specific outreach email DRAFT.

    Returns a plain dict (never a stored outreach record). ``used_ai`` says
    whether the validated model output was used; ``used_story`` whether the
    email had a song story to draw on. Nothing here sends, records, or
    invents — see the module docstring.
    """
    payload = payload or {}
    recipient_block = payload.get("recipient")
    recipient_block = recipient_block if isinstance(recipient_block, dict) else {}
    artist_block = payload.get("artist")
    artist_block = artist_block if isinstance(artist_block, dict) else {}

    title = _song_title(track)
    artist = _clean(artist_block.get("name"))
    recipient = _clean(recipient_block.get("name"))
    role = _clean(recipient_block.get("role"))
    organization = _clean(recipient_block.get("organization"))
    objective = _clean(payload.get("objective"))
    instructions = _clean(payload.get("instructions"))
    url = _clean(track.get("listen_url"))
    story = _resolve_story(track, payload)
    style = _resolve_style(payload)
    used_story = bool(story)

    warnings: list[str] = []
    if not used_story:
        warnings.append(
            "No song story is saved for this release; the draft is based on "
            "release metadata only and does not describe the song's meaning.")
    if not url:
        warnings.append(
            "No listening URL is saved for this release; the email does not "
            "include a listening link.")
    if not title:
        warnings.append("The release has no readable title.")

    subject = ""
    message = ""
    used_ai = False
    model = None
    prompt_version = None

    if client is not None:
        try:
            available = client.available()
        except Exception:  # noqa: BLE001 — availability must never raise here
            available = False
        if available:
            try:
                template = load_template(PROMPT_DOMAIN, PROMPT_NAME)
                result = client.generate(
                    template,
                    song_title=title,
                    artist_name=artist,
                    recipient_name=recipient,
                    recipient_role=role,
                    organization=organization,
                    song_story=story,
                    listening_url=url,
                    objective=objective,
                    style=STYLES[style],
                    instructions=instructions,
                )
            except TemplateError:
                result = None
            if result is not None and result.ok \
                    and isinstance(result.data, dict):
                subject = _clean(result.data.get("subject"))
                message = _clean(result.data.get("message"))
                if subject and message:
                    used_ai = True
                    model = result.model
                    prompt_version = result.prompt_version

    if not used_ai:
        subject, message = _deterministic_draft(
            title=title, artist=artist, recipient=recipient, role=role,
            organization=organization, story=story, url=url,
            objective=objective)
        if not subject:
            subject = (f"New release from {artist}: {title}"
                       if artist and title
                       else f"New release: {title}" if title
                       else "New release for your consideration")

    link_included = bool(url) and (url in message)
    if url and not link_included:
        message = _ensure_link(message, url)
        link_included = url in message

    return {
        "subject": subject,
        "message": message,
        "style": style,
        "used_ai": used_ai,
        "used_story": used_story,
        "model": model,
        "prompt_version": prompt_version,
        "listening_url": url or None,
        "link_included": link_included,
        "song_title": title or None,
        "warnings": warnings,
    }
