#!/usr/bin/env python3
"""Fetch a URL and extract structured records from it in one step.

v0.8.21 — prefer this over chaining fetch_html/web_read + extract_records when
you start from a URL. Reuses the v0.8.20 polite headers so free APIs that block
the default python-requests UA (Nominatim, Overpass) actually serve the page.

T3 ships single-page; T4 adds lightweight pagination (max_pages cap 5).
"""
from __future__ import annotations

TOOL_META = {
    "name": "web_extract",
    "tool_type": "web",
    "dependencies": ["requests", "jsonschema"],
}

_DEFAULT_UA = "systemu/0.8 (+https://github.com/rameswaran-mohan/project-systemu)"
_DEFAULT_ACCEPT = "text/html,application/json,*/*"
_MIN_BODY_CHARS = 100
_MIN_TEXT_CHARS = 50


def _fetch(url: str, headers: dict, params: dict, timeout: int):
    import requests
    h = dict(headers or {})
    h.setdefault("User-Agent", _DEFAULT_UA)
    h.setdefault("Accept", _DEFAULT_ACCEPT)
    r = requests.get(url, headers=h, params=params or {}, timeout=timeout)
    return r


import re as _re

_HARD_MAX_PAGES = 5

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


def run(**kwargs) -> dict:
    url: str = kwargs.get("url", "") or ""
    schema = kwargs.get("schema") or {}
    max_records: int = int(kwargs.get("max_records", 20))
    max_pages: int = max(1, min(int(kwargs.get("max_pages", 1)), _HARD_MAX_PAGES))
    timeout: int = int(kwargs.get("timeout", 30))
    headers: dict = kwargs.get("headers", {}) or {}
    params: dict = kwargs.get("params", {}) or {}

    if not url:
        return {"success": False, "records": [], "count": 0,
                "error_type": "bad_request", "error": "url is required",
                "pages_fetched": 0}

    from systemu.runtime.extractor import extract_records, _sanitize_html

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

    return {"success": True, "records": accumulated, "count": len(accumulated),
            "status_code": last_status, "pages_fetched": pages_fetched,
            "error": None}
