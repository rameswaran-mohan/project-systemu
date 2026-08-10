#!/usr/bin/env python3
"""Read a web page: T0 httpx fetch first; escalate to headless browser for JS/SPA pages."""
from __future__ import annotations

import os

# DEPENDENCIES: none, and that is a CORRECTION, not a relaxation.
#
# This manifest declared ["playwright"] and the declaration was wrong even
# before v0.10.24 made playwright optional. `web_read`'s primary paths are the
# Jina Reader and a raw GET (web_access.read_url) — pure HTTP, no browser.
# Chromium is only the LAST-RESORT render escalation for JS/anti-bot pages,
# and web_access._browser_render already treats its absence as a skipped tier.
#
# Leaving the declaration in place would have been actively harmful once
# playwright left the core install: `dependency_installer.ensure_satisfied`
# runs BEFORE the tool body, so a machine without the [browser] extra would
# have refused to run web_read at all — a tool that works perfectly over HTTP
# reported UNAVAILABLE and never invoked. A false unavailability is the same
# defect as a false readiness, pointed the other way.
#
# web_act and web_screenshot DO still declare it: both go straight to
# BrowserPool with no non-browser path.
TOOL_META = {"name": "web_read", "tool_type": "web", "dependencies": []}

# v0.9.8 Phase 1 Task 7: gate the keyless web_access stack on the env var so we
# don't depend on a Config object. Default ON.
_V2 = os.getenv("SYSTEMU_WEB_STACK_V2", "true").lower() != "false"


def run(**kwargs) -> dict:
    url = kwargs.get("url", "")
    if not url:
        return {"success": False, "title": "", "text": "", "links": [], "error": "url is required"}

    # v0.9.8 Phase 1 Task 7: under SYSTEMU_WEB_STACK_V2 (default on), delegate to
    # the keyless web_access layer. read_url(..., render=True) preserves the
    # JS/anti-bot escalation (Jina Reader → raw GET → Chromium-stealth render).
    # The legacy fetch_core/BrowserPool path is kept verbatim under `else`.
    if os.getenv("SYSTEMU_WEB_STACK_V2", "true").lower() != "false":
        from systemu.runtime import web_access
        from systemu.runtime.web import fetch_core
        res = web_access.read_url(url, render=True)
        content = res.get("content") or ""
        if content:
            parsed = fetch_core.extract_readable(content, url)
            # source: jina/raw/browser → keep the spirit of tier_used.
            return {"success": True, "tier_used": res.get("source") or "web_access",
                    **parsed, "error": None}
        return {"success": False, "title": "", "text": "", "links": [],
                "error": res.get("error") or "all backends failed",
                "tier_used": res.get("source") or "web_access"}

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
        # F21: the render tier needs the [browser] extra even though the tool as
        # a whole does not. Say WHICH tier was unavailable and how to get it —
        # "all backends failed" would hide a one-command fix.
        from systemu.runtime.optional_deps import OptionalDependencyMissing
        if isinstance(exc, OptionalDependencyMissing):
            return {"success": False, "title": "", "text": "", "links": [],
                    "error": (f"The HTTP tiers could not read this page and the "
                              f"JS-render tier is not installed. {exc}"),
                    "error_type": "capability_unavailable",
                    "missing_packages": list(exc.packages),
                    "tier_used": "browser"}
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            return {"success": False, "title": "", "text": "", "links": [],
                    "error": "browser not ready yet (chromium installing)",
                    "error_type": "missing_dependency", "missing_packages": ["playwright-chromium"],
                    "tier_used": "browser"}
        return {"success": False, "title": "", "text": "", "links": [], "error": str(exc), "tier_used": "browser"}
