#!/usr/bin/env python3
"""Read a web page: T0 httpx fetch first; escalate to headless browser for JS/SPA pages."""
from __future__ import annotations

TOOL_META = {"name": "web_read", "tool_type": "web", "dependencies": ["playwright"]}


def run(**kwargs) -> dict:
    url = kwargs.get("url", "")
    if not url:
        return {"success": False, "title": "", "text": "", "links": [], "error": "url is required"}
    from systemu.runtime.web import fetch_core
    res = fetch_core.fetch_url(url)
    if res.ok:
        parsed = fetch_core.extract_readable(res.html, url)
        if not fetch_core.looks_like_spa(res.html, parsed["text"]):
            return {"success": True, "tier_used": "fetch", **parsed, "error": None}
    # Escalate to T2 (JS render)
    try:
        from systemu.runtime.web.browser_pool import BrowserPool
        html = BrowserPool.get().render_html(url)
        parsed = fetch_core.extract_readable(html, url)
        return {"success": True, "tier_used": "browser", **parsed, "error": None}
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            return {"success": False, "title": "", "text": "", "links": [],
                    "error": "browser not ready yet (chromium installing)",
                    "error_type": "missing_dependency", "missing_packages": ["playwright-chromium"],
                    "tier_used": "browser"}
        return {"success": False, "title": "", "text": "", "links": [], "error": str(exc), "tier_used": "browser"}
