"""R-B5 / T5 §5.10.b#6 + §5.10.e AC4b — capture exclusion for systemu's OWN surfaces.

    "`/table` (especially the Keys zone) and the chat strip are excluded from
     evidence/attest/`web_act`/screenshot capture."  — §5.10.b#6

`/table` renders the operator's whole curated world: service accounts, folder
paths, and the Keys zone (credential NAMES). A task that screenshots or drives it
would persist that inventory into an artifact — and, via `web_act`, could operate
the board's own controls. This module is the single predicate that says no.

WHY THIS IS NOT ALREADY COVERED BY THE SSRF GUARD (do not delete it as redundant).
``net_safety.url_is_admissible`` rejects loopback, so it does block the DEFAULT
dashboard origin — but it is a different rule with a different reason, and the
overlap is a coincidence of the default bind, not a guarantee:

  * ``allowed_outbound_hosts()`` is operator-configurable and can whitelist a host;
  * ``SYSTEMU_DASHBOARD_HOST`` can be a LAN address, which is not loopback and is
    admissible on any deployment whose allow-list permits it.

Either configuration leaves an SSRF-clean URL pointing straight at `/table`. So
the exclusion is stated as its own named rule, checked independently, and pinned
by its own tests. A capture is refused when EITHER rule fires.

SCOPE — stated, not silent (the §5.10.e AC1 caveat pattern). This is a URL-origin
rule, so it covers exactly the URL-addressed capture paths: `web_screenshot`,
`web_act`, and browser-driven evidence. It CANNOT cover a full-desktop
``take_screenshot``, which grabs framebuffer pixels with no URL to inspect — if
`/table` is the foreground window, those pixels are captured. That gap is real,
is NOT closed here, and needs a window-level filter (the v0.9.32 recorder's
Layer-1 self-filter is the nearest existing precedent).
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Loopback spellings treated as one canonical host, so a 127.0.0.1-stamped origin
#: still matches a tab the browser opened as ``localhost``.
_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}


def _canon_host(host: Optional[str]) -> str:
    h = (host or "").lower()
    return "localhost" if h in _LOOPBACK else h


def dashboard_origin() -> str:
    """systemu's own dashboard origin, from the env the dashboard stamps at startup.

    Canonical implementation — ``interface.command.dispatch._dashboard_origin``
    delegates here so the recorder's self-filter and this capture exclusion can
    never drift onto two different notions of "our own UI".
    """
    pre = os.environ.get("SYSTEMU_DASHBOARD_ORIGIN")
    if pre:
        return pre
    host = os.environ.get("SYSTEMU_DASHBOARD_HOST") or "localhost"
    if host in ("0.0.0.0", "::", ""):
        host = "localhost"
    port = os.environ.get("SYSTEMU_DASHBOARD_PORT") or "8765"
    return f"http://{host}:{port}"


def is_own_surface(url: str) -> bool:
    """True if ``url`` is served by systemu's own dashboard (scheme+host+port match).

    Origin-scoped, NOT path-scoped. `/table` is the sensitive page the AC names, but
    a path check would be trivially defeated (``/table?x=1``, a redirect, a deep
    link) and the neighbouring pages leak the same inventory — the chat strip
    (§5.10.b#6 names it), `/tools`, the Connections UI. Excluding the whole origin
    is both simpler to reason about and strictly safer, and matches what the
    v0.9.32 recorder self-filter already does.

    Fail-CLOSED on an unparseable URL: if we cannot tell what we are about to
    capture, we refuse. Never raises.
    """
    try:
        if not url:
            return False
        origin = dashboard_origin()
        u, o = urlsplit(url), urlsplit(origin)
        if not u.scheme and not u.netloc:
            return False          # not a URL at all (a file path) — not our surface
        return (_canon_host(u.hostname), u.port or _default_port(u.scheme)) == \
               (_canon_host(o.hostname), o.port or _default_port(o.scheme))
    except Exception:
        logger.debug("[capture_exclusion] unparseable URL — refusing", exc_info=True)
        return True               # fail closed


def _default_port(scheme: str) -> Optional[int]:
    return {"http": 80, "https": 443}.get((scheme or "").lower())


def refusal_reason(url: str) -> Optional[str]:
    """The refusal string for a capture of ``url``, or None when it is permitted.

    Returned rather than raised so each caller refuses in its own idiom (a tool
    returns an error dict; the pool raises PermissionError) while the WORDING —
    which is what an operator and a log reader actually see — stays in one place.
    """
    if is_own_surface(url):
        return ("Refusing to capture systemu's own interface "
                f"({url}) — §5.10.b#6 capture exclusion: the table and chat "
                "surfaces render your inventory and credential names, and must "
                "never enter an evidence, attest, or screenshot artifact.")
    return None
