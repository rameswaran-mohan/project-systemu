#!/usr/bin/env python3
"""browser_navigate — Navigate the active browser page to a URL.

Parameters (via run() kwargs):
  url (str, required): The fully-qualified URL to navigate to.

Returns (dict):
  success (bool): True if navigation completed without error.
  url     (str):  The URL that was navigated to (echoed back).
  error   (str|None): Error message on failure, otherwise None.
"""
from __future__ import annotations

TOOL_META = {
    "name": "browser_navigate",
    "tool_type": "browser_action",
    "dependencies": ["playwright"],
}

_PLAYWRIGHT_MISSING_SIGNALS = (
    "Executable doesn't exist",
    "playwright install",
    "BrowserType.launch",
    "No module named 'playwright'",
    "No module named \"playwright\"",
)


def _is_playwright_missing(exc: Exception) -> bool:
    msg = str(exc)
    return any(s in msg for s in _PLAYWRIGHT_MISSING_SIGNALS)


def run(url: str) -> dict:
    """Navigate an existing Chrome CDP session (or a fresh Chromium instance) to url."""
    if not url:
        return {"success": False, "url": url, "error": "url parameter is required"}

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

        try:
            with sync_playwright() as p:
                # Prefer an existing Chrome DevTools Protocol session (e.g. launched
                # by another tool or by the user).  Fall back to launching headless.
                try:
                    browser = p.chromium.connect_over_cdp("http://localhost:9222")
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.pages[0] if context.pages else context.new_page()
                except Exception:
                    browser = p.chromium.launch(headless=False)
                    page = browser.new_page()

                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                return {"success": True, "url": url, "error": None}

        except PlaywrightTimeoutError:
            return {"success": False, "url": url, "error": f"Timeout navigating to: {url}"}

    except Exception as exc:
        if _is_playwright_missing(exc):
            return {
                "success": False,
                "url": url,
                "error": str(exc),
                "error_type": "missing_dependency",
                "fix": "Run: playwright install chromium",
            }
        return {"success": False, "url": url, "error": str(exc)}
