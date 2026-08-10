#!/usr/bin/env python3
"""Capture a full-page screenshot via headless chromium."""
from __future__ import annotations

TOOL_META = {"name": "web_screenshot", "tool_type": "browser_action", "dependencies": ["playwright"]}


def run(**kwargs) -> dict:
    url = kwargs.get("url", "")
    output_path = kwargs.get("output_path", "") or "screenshot.png"
    if not url:
        return {"success": False, "image_path": "", "error": "url is required"}
    try:
        from systemu.runtime.web.browser_pool import BrowserPool
        path = BrowserPool.get().screenshot(url, output_path)
        return {"success": True, "image_path": path, "tier_used": "browser", "error": None}
    except Exception as exc:
        # F21: the [browser] extra is not installed. `str(exc)` is already the
        # full remedy (optional_deps builds it), but the TYPE is what lets the
        # runtime stop retrying — this is a capability the operator has not
        # installed, not a transient failure.
        from systemu.runtime.optional_deps import OptionalDependencyMissing
        if isinstance(exc, OptionalDependencyMissing):
            return {"success": False, "image_path": "", "error": str(exc),
                    "error_type": "capability_unavailable",
                    "missing_packages": list(exc.packages), "retryable": False}
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            return {"success": False, "image_path": "", "error": "browser not ready (chromium installing)",
                    "error_type": "missing_dependency", "missing_packages": ["playwright-chromium"]}
        return {"success": False, "image_path": "", "error": str(exc)}
