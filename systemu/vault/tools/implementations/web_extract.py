#!/usr/bin/env python3
"""Fetch a URL and extract structured records from it in one step.

v0.8.21 — prefer this over chaining fetch_html/web_read + extract_records when
you start from a URL. Reuses the v0.8.20 polite headers so free APIs that block
the default python-requests UA (Nominatim, Overpass) actually serve the page.

T3 ships single-page; T4 adds lightweight pagination (max_pages cap 5).

LLM-usability fix (post-v0.8.22.1) — accept a simpler ``fields=["name","url"]``
interface in addition to the full ``schema=`` JSON Schema, and fall back to a
schemaless heuristic when neither is supplied. The previous interface required
the LLM to construct a JSON Schema on every call; in the live v0.8.22.1 daemon
log against "find top burrito places near me" the model kept omitting
``schema`` entirely and tripping Gate 2.5 (tool_param_invalid) until the stuck
guard fired. With ``fields``, the LLM can say ``fields=["name","url","rating"]``
and the runtime builds the JSON Schema internally.
"""
from __future__ import annotations

import logging
import re as _re

logger = logging.getLogger(__name__)

TOOL_META = {
    "name": "web_extract",
    "tool_type": "web",
    "dependencies": ["requests", "jsonschema"],
}

# v0.9.1.1: browser-realistic UA + headers so sites that block scrapers
# (Yelp, Reddit, TripAdvisor, Google, etc.) actually return content. The
# old "systemu/0.8 (+github.com/...)" UA was a dead giveaway and was being
# 403'd by every major site — surfaced by the v0.9.1 burrito live test.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_DEFAULT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8"
)
_DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"
_MIN_BODY_CHARS = 100
_MIN_TEXT_CHARS = 50


def _fetch(url: str, headers: dict, params: dict, timeout: int):
    import requests
    h = dict(headers or {})
    # setdefault — caller-supplied headers always win.
    h.setdefault("User-Agent", _DEFAULT_UA)
    h.setdefault("Accept", _DEFAULT_ACCEPT)
    h.setdefault("Accept-Language", _DEFAULT_ACCEPT_LANGUAGE)
    h.setdefault("DNT", "1")
    h.setdefault("Connection", "keep-alive")
    h.setdefault("Upgrade-Insecure-Requests", "1")
    r = requests.get(url, headers=h, params=params or {}, timeout=timeout)
    return r


_HARD_MAX_PAGES = 5

# v0.9.1.1: status codes that almost certainly mean "anti-bot detection
# blocked us." When we hit one of these, the error message tells the LLM
# to retry against a search engine instead of banging on the same URL.
# We can't bypass Cloudflare / PerimeterX / Akamai Bot Manager from
# requests alone — that needs JS rendering (Playwright, deferred to L6).
_ANTI_BOT_STATUS = frozenset({401, 403, 406, 429, 451})

_ANTI_BOT_HINT = (
    "Looks like anti-bot/scraper detection (Cloudflare, PerimeterX, etc.). "
    "This site requires a real browser with JavaScript. "
    "Retry with a search-engine URL — these usually work: "
    "https://duckduckgo.com/html/?q=<your+query> or "
    "https://www.google.com/search?q=<your+query>. "
    "Then web_extract the search-result page and follow promising links "
    "to general directories or Wikipedia pages."
)

_REL_NEXT_HREF_RE = _re.compile(
    r'<(?:link|a)[^>]*\brel=["\']?next["\']?[^>]*\bhref=["\']([^"\']+)["\']',
    _re.IGNORECASE,
)
_HREF_REL_NEXT_RE = _re.compile(
    r'<(?:link|a)[^>]*\bhref=["\']([^"\']+)["\'][^>]*\brel=["\']?next["\']?',
    _re.IGNORECASE,
)
_PAGE_PARAM_RE = _re.compile(r'([?&])(p(?:age)?)=(\d+)', _re.IGNORECASE)


def _next_url(current_url: str, body: str) -> "str | None":
    """v0.8.21: detect a next-page URL.

    Two patterns (priority): (1) <link/a rel="next" href="..."> OR
    <link/a href="..." rel="next">; (2) ?page=N in URL.
    Returns the next URL or None. No JS-based pagination."""
    body = body or ""
    m = _REL_NEXT_HREF_RE.search(body) or _HREF_REL_NEXT_RE.search(body)
    if m:
        href = m.group(1).strip()
        if href.startswith("http://") or href.startswith("https://"):
            return href
        # Use urljoin for safe relative-to-absolute resolution
        from urllib.parse import urljoin
        return urljoin(current_url, href)
    m2 = _PAGE_PARAM_RE.search(current_url)
    if m2:
        sep, key, n = m2.group(1), m2.group(2), int(m2.group(3))
        return _PAGE_PARAM_RE.sub(f"{sep}{key}={n+1}", current_url, count=1)
    return None


def _dedup_key(record: dict, schema: dict) -> str:
    if not isinstance(record, dict):
        return repr(record)
    required = (schema.get("required") or []) if isinstance(schema, dict) else []
    field = required[0] if required else "name"
    return str(record.get(field, repr(record)))


# Heuristic for which field names probably hold numbers when the LLM gives us
# bare `fields`. Kept small and obvious so the LLM can predict the behavior.
_NUMERIC_FIELD_HINTS = {
    "rating", "price", "cost", "count", "score", "votes", "reviews",
    "stars", "year", "amount", "number", "qty", "quantity",
}


def _infer_type(field_name: str) -> str:
    """Map a bare field name to a JSON Schema type for the heuristic builder."""
    n = (field_name or "").strip().lower()
    if n in _NUMERIC_FIELD_HINTS:
        return "number"
    return "string"


def _schema_from_fields(fields: "list[str]") -> dict:
    """Build a minimal JSON Schema for ONE record from a flat list of field names.

    Field-name-to-type rules:
      * names in ``_NUMERIC_FIELD_HINTS`` (rating, price, count, ...) -> ``number``
      * everything else -> ``string``

    The first field becomes ``required`` so the dedup key in the paginated path
    stays meaningful (matches the legacy ``required[0] or "name"`` heuristic)."""
    cleaned = [str(f).strip() for f in (fields or []) if str(f or "").strip()]
    properties = {name: {"type": _infer_type(name)} for name in cleaned}
    required = [cleaned[0]] if cleaned else []
    return {"type": "object", "properties": properties, "required": required}


# ── Heuristic schemaless extraction ────────────────────────────────────────

# Tags we consider plausible "card" containers when scanning for repeated
# structure. Kept narrow because the wider we cast, the more false positives
# we get (every <div> in a page is meaningless).
_CARD_TAGS = ("li", "article", "section", "div", "tr")
_CARD_TAG_RE = {
    tag: _re.compile(
        rf'<{tag}\b[^>]*>(.*?)</{tag}>',
        _re.IGNORECASE | _re.DOTALL,
    )
    for tag in _CARD_TAGS
}
_ANCHOR_RE = _re.compile(
    r'<a\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    _re.IGNORECASE | _re.DOTALL,
)
_TAG_STRIP_RE = _re.compile(r'<[^>]+>')
_WS_RE = _re.compile(r'\s+')
_PRICE_RE = _re.compile(r'(?:[$£€¥])\s*(\d+(?:\.\d+)?)')
_RATING_RE = _re.compile(r'\b([0-5](?:\.\d)?)\s*/?\s*5?\b')


def _strip_tags(s: str) -> str:
    return _WS_RE.sub(" ", _TAG_STRIP_RE.sub(" ", s or "")).strip()


def _heuristic_extract(html: str, *, max_records: int = 20) -> "list[dict]":
    """Schemaless fallback: find the most common container tag, then for each
    container pull a best-effort {name/title, url/href, price, rating,
    description} record.

    No LLM call, no jsonschema dependency. Designed so that when the LLM
    forgets both ``schema`` and ``fields`` the tool still returns SOMETHING
    usable instead of hard-erroring back into a loop."""
    html = html or ""
    if not html:
        return []

    # Score each candidate container tag by how many matches it produces.
    # The winner is the tag with the most repeated children — that's almost
    # always the list of cards.
    best_tag = None
    best_matches: "list[str]" = []
    for tag, rx in _CARD_TAG_RE.items():
        matches = rx.findall(html)
        # Drop trivially small fragments (nav items, single icons, etc).
        matches = [m for m in matches if len(_strip_tags(m)) >= 20]
        if len(matches) > len(best_matches):
            best_tag = tag
            best_matches = matches

    if not best_matches:
        return []

    records: "list[dict]" = []
    for raw in best_matches[:max_records]:
        rec: dict = {}

        # First anchor inside the card → url + name.
        a = _ANCHOR_RE.search(raw)
        if a:
            href = a.group(1).strip()
            anchor_text = _strip_tags(a.group(2))
            if href:
                rec["url"] = href
            if anchor_text:
                rec["name"] = anchor_text

        # Visible text in the card → description (capped).
        text = _strip_tags(raw)
        if text and "description" not in rec:
            rec["description"] = text[:240]

        # Price + rating regexes are cheap; only emit if we actually found one.
        pm = _PRICE_RE.search(text)
        if pm:
            try:
                rec["price"] = float(pm.group(1))
            except ValueError:
                pass
        rm = _RATING_RE.search(text)
        if rm:
            try:
                rec["rating"] = float(rm.group(1))
            except ValueError:
                pass

        if rec:
            records.append(rec)

    # Helpful debug breadcrumb so an operator can see what the heuristic chose.
    logger.debug(
        "[web_extract] heuristic chose tag=<%s> with %d candidates, kept %d records",
        best_tag, len(best_matches), len(records),
    )
    return records


def run(**kwargs) -> dict:
    url: str = kwargs.get("url", "") or ""
    # v0.8.22-post fix: accept either a flat `fields` list OR a full `schema`
    # JSON Schema. Explicit `schema` wins when both are supplied (advanced
    # callers occasionally pass both during migration).
    schema_in = kwargs.get("schema")
    fields_in = kwargs.get("fields")
    max_records: int = int(kwargs.get("max_records", 20))
    max_pages: int = max(1, min(int(kwargs.get("max_pages", 1)), _HARD_MAX_PAGES))
    timeout: int = int(kwargs.get("timeout", 30))
    headers: dict = kwargs.get("headers", {}) or {}
    params: dict = kwargs.get("params", {}) or {}

    if not url:
        return {"success": False, "records": [], "count": 0,
                "error_type": "bad_request", "error": "url is required",
                "pages_fetched": 0}

    # Resolve the effective schema, if any. Three branches:
    #   1. explicit schema dict → use as-is (back-compat, LLM-built schemas).
    #   2. only `fields` given  → synthesize a minimal JSON Schema.
    #   3. neither              → heuristic extraction (no schema, no LLM).
    schema: dict = {}
    mode = "schema"
    if isinstance(schema_in, dict) and schema_in:
        schema = schema_in
        mode = "schema"
    elif isinstance(fields_in, list) and any(str(f or "").strip() for f in fields_in):
        schema = _schema_from_fields(fields_in)
        mode = "fields"
        logger.info(
            "[web_extract] built JSON Schema from fields=%s",
            [str(f).strip() for f in fields_in if str(f or "").strip()],
        )
    else:
        mode = "heuristic"

    from systemu.runtime.extractor import _sanitize_html

    accumulated: list = []
    seen_keys: set = set()
    pages_fetched = 0
    last_status = 0
    current_url = url
    last_err = None

    for page_i in range(max_pages):
        try:
            resp = _fetch(current_url, headers, params, timeout)
        except Exception as exc:
            last_err = ("fetch_error", str(exc), 0)
            break
        pages_fetched += 1
        last_status = resp.status_code

        if resp.status_code >= 400:
            # v0.9.1.1: anti-bot status codes get a concrete retry hint so
            # the LLM doesn't sit and re-extract the same URL. Returns a
            # different error_type so the integrity guard's downstream
            # logging can distinguish "site is offline" from "site refuses bots."
            if resp.status_code in _ANTI_BOT_STATUS:
                last_err = (
                    "anti_bot_blocked",
                    f"HTTP {resp.status_code}. {_ANTI_BOT_HINT}",
                    resp.status_code,
                )
            else:
                last_err = ("http_error", f"HTTP {resp.status_code}", resp.status_code)
            break

        body = resp.text or ""
        if len(body) < _MIN_BODY_CHARS or len(_sanitize_html(body)) < _MIN_TEXT_CHARS:
            if pages_fetched == 1 and not accumulated:
                return {"success": False, "records": [], "count": 0,
                        "error_type": "empty_or_blocked",
                        "note": "fetched but no extractable content; try a different source",
                        "status_code": resp.status_code,
                        "pages_fetched": pages_fetched}
            break  # later page is empty — stop, return what we have

        if mode == "heuristic":
            page_records = _heuristic_extract(body, max_records=max_records)
            page_out = {"success": True, "records": page_records,
                        "count": len(page_records), "error": None}
        else:
            # Local import keeps the heuristic path independent of the LLM
            # extractor module (which pulls in llm_router on import).
            from systemu.runtime.extractor import extract_records
            page_out = extract_records(body, schema, max_records=max_records)

        if page_out.get("success") and page_out.get("records"):
            for rec in page_out["records"]:
                k = _dedup_key(rec, schema)
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                accumulated.append(rec)
                if len(accumulated) >= max_records:
                    break
        elif pages_fetched == 1 and not page_out.get("success"):
            # first page failed — propagate the extractor's degraded shape
            out = dict(page_out)
            out["status_code"] = resp.status_code
            out["pages_fetched"] = pages_fetched
            return out

        if len(accumulated) >= max_records:
            break

        nxt = _next_url(current_url, body)
        if not nxt:
            break
        current_url = nxt

    if not accumulated and last_err:
        kind, msg, sc = last_err
        return {"success": False, "records": [], "count": 0,
                "error_type": kind, "error": msg,
                "status_code": sc, "pages_fetched": pages_fetched}

    if mode == "heuristic":
        # Loud INFO line the task brief asked for, so the operator sees this
        # path was taken without having to grep DEBUG.
        logger.info(
            "[web_extract] no schema/fields supplied - used heuristic extraction "
            "(got %d records)", len(accumulated),
        )

    return {"success": True, "records": accumulated, "count": len(accumulated),
            "status_code": last_status, "pages_fetched": pages_fetched,
            "error": None}
