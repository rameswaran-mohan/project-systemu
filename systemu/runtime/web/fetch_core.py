"""T0 — dependency-free web fetch + readability extraction.

Uses httpx (core dep). Pure-Python text extraction via stdlib html.parser —
no beautifulsoup. Works the instant the daemon boots on a bare pip install.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

_MAX_BYTES = 5 * 1024 * 1024
_SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "noscript"}


@dataclass
class FetchResult:
    ok: bool
    status: int
    html: str = ""
    error: Optional[str] = None


class _Readable(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self._base = base_url
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self._text: List[str] = []
        self.links: List[Dict[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append({"url": urljoin(self._base, v), "text": ""})

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth == 0:
            s = data.strip()
            if s:
                self._text.append(s)

    @property
    def text(self) -> str:
        return " ".join(self._text)


def extract_readable(html: str, base_url: str) -> Dict[str, Any]:
    """Strip chrome/scripts, return {title, text, links}."""
    p = _Readable(base_url)
    try:
        p.feed(html)
    except Exception:
        logger.debug("[fetch_core] parse error — returning partial", exc_info=True)
    return {"title": p.title, "text": p.text, "links": p.links}


def looks_like_spa(html: str, extracted_text: str) -> bool:
    """Heuristic: little text + SPA-shell markers OR heavy script count."""
    if len(extracted_text) >= 200:
        return False
    shell = ('id="root"' in html or 'id="app"' in html)
    script_count = html.count("<script")
    return shell or script_count > 5


def fetch_url(url: str, timeout: int = 20) -> FetchResult:
    """httpx GET with realistic headers, size + content-type guards.

    R-A11: SSRF-gated like the v2 web_access seam — the initial URL AND every
    redirect hop are re-checked against ``net_safety`` (redirects are followed
    manually so a public URL cannot 302 to an internal/metadata host)."""
    import httpx

    from systemu.runtime import net_safety
    _allow = net_safety.allowed_outbound_hosts()
    if not net_safety.url_is_admissible(url, allowed_hosts=_allow):
        return FetchResult(ok=False, status=0,
                           error="blocked: destination is not an allowed public address (SSRF guard)")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SystemuBot/1.0; +https://systemu.local)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        # follow_redirects=False + a MANUAL, re-gated hop loop (httpx's own follow
        # would chase a redirect to an internal host without re-checking).
        with httpx.Client(follow_redirects=False, timeout=timeout, headers=headers) as c:
            r = c.get(url)
            _hops = 0
            while r.is_redirect and _hops < 5:
                _loc = r.headers.get("location", "")
                _nxt = str(r.url.join(_loc)) if _loc else ""
                if not _nxt or not net_safety.url_is_admissible(_nxt, allowed_hosts=_allow):
                    return FetchResult(ok=False, status=r.status_code,
                                       error="blocked: redirect to a non-public address (SSRF guard)")
                r = c.get(_nxt)
                _hops += 1
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype and "json" not in ctype:
                return FetchResult(ok=False, status=r.status_code,
                                   error=f"unsupported content-type: {ctype}")
            body = r.text[:_MAX_BYTES]
            return FetchResult(ok=r.status_code < 400, status=r.status_code, html=body,
                               error=None if r.status_code < 400 else f"HTTP {r.status_code}")
    except Exception as exc:
        return FetchResult(ok=False, status=0, error=str(exc))
