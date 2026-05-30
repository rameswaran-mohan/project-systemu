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
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            return {"success": False, "image_path": "", "error": "browser not ready (chromium installing)",
                    "error_type": "missing_dependency", "missing_packages": ["playwright-chromium"]}
        return {"success": False, "image_path": "", "error": str(exc)}
