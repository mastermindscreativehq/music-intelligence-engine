"""HTML page parsing: links, title, visible text, mailto addresses.

Built on the stdlib ``html.parser.HTMLParser`` — no third-party dependency.
Script/style content is ignored. Link hrefs are resolved against the page URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin

from html.parser import HTMLParser


@dataclass
class PageLink:
    href_absolute: str
    anchor_text: str
    is_mailto: bool


@dataclass
class ParsedPage:
    url: str
    title: str = ""
    text: str = ""
    links: list[PageLink] = field(default_factory=list)
    mailtos: list[str] = field(default_factory=list)
    # <form> action URLs (absolute) — evidence of an on-page submission form.
    forms: list[str] = field(default_factory=list)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []       # (href, anchor text)
        self.mailtos: list[str] = []
        self.forms: list[str] = []                   # form action URLs
        self._skip_depth = 0                          # inside script/style
        self._in_title = False
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._page_url: str = ""

    # Block-level tags that implicitly terminate an unterminated <title>
    # (mirrors browser recovery for malformed documents).
    _TITLE_TERMINATORS = {"body", "p", "div", "ul", "ol", "li", "table",
                          "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr",
                          "a", "header", "nav", "main", "section"}

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth += 1
            return
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        else:
            if self._in_title and tag in self._TITLE_TERMINATORS:
                self._in_title = False   # malformed markup recovery
            if tag == "form":
                action = attrs_dict.get("action", "").strip()
                if action:
                    try:
                        absolute = urljoin(self._page_url, action)
                    except Exception:
                        absolute = ""
                    if absolute.startswith(("http://", "https://")):
                        self.forms.append(absolute)
                else:
                    # Form without action posts to the current page —
                    # still evidence of an on-page form.
                    self.forms.append("")
            elif tag == "a":
                href = attrs_dict.get("href", "").strip()
                if href:
                    low = href.lower()
                    if low.startswith("mailto:"):
                        self.mailtos.append(href[len("mailto:"):])
                    else:
                        self._anchor_href = href
                        self._anchor_text = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title" and self._in_title:
            self._in_title = False
        elif tag == "a" and self._anchor_href is not None:
            anchor = " ".join("".join(self._anchor_text).split())
            self.links.append((self._anchor_href, anchor))
            self._anchor_href = None
            self._anchor_text = []

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._in_title:
            combined = "".join(self.title_parts) + data
            # Unterminated-<title> recovery: html.parser treats <title>
            # content as raw text, so a missing </title> swallows the whole
            # document as one chunk. A real title never contains raw markup
            # nor exceeds sane length — if it does, abandon the title and
            # process this chunk (and the rest) as body content.
            if "<" in combined or len(combined) > 300:
                self.title_parts = []
                self._in_title = False
            else:
                self.title_parts.append(data)
                return
        if data.strip():
            self.text_parts.append(data.strip())
        if self._anchor_href is not None and data.strip():
            self._anchor_text.append(data)


def parse_html(page_url: str, html: str) -> ParsedPage:
    """Parse *html* obtained from *page_url* into a :class:`ParsedPage`."""
    parser = _PageParser()
    parser._page_url = page_url
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # Malformed HTML must not crash discovery; keep whatever was parsed.
        pass
    parsed = ParsedPage(url=page_url)
    parsed.title = " ".join("".join(parser.title_parts).split())
    parsed.text = "\n".join(parser.text_parts)
    for href, anchor in parser.links:
        try:
            absolute = urljoin(page_url, href.strip())
        except Exception:
            continue
        if absolute.startswith(("http://", "https://")):
            parsed.links.append(PageLink(absolute, anchor, False))
    for raw in parser.mailtos:
        address = raw.split("?", 1)[0].strip()
        if address:
            parsed.mailtos.append(address)
    parsed.forms = list(parser.forms)
    return parsed
