#!/usr/bin/env python3
"""Extract visible text from a web page using Playwright."""
from __future__ import annotations

TOOL_META = {
    "name": "web_extract_text",
    "tool_type": "web",
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


def run(**kwargs) -> dict:
    url: str = kwargs.get("url", "")
    selector: str = kwargs.get("selector", "body")

    if not url:
        return {"success": False, "text": "", "error": "url is required"}

    if not selector:
        selector = "body"

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")

            try:
                text = page.inner_text(selector)
            except Exception:
                text = page.inner_text("body")

            browser.close()

        return {"success": True, "text": text, "error": None}

    except Exception as exc:
        if _is_playwright_missing(exc):
            return {
                "success": False,
                "text": "",
                "error": str(exc),
                "error_type": "missing_dependency",
                "fix": "Run: playwright install chromium",
            }
        return {"success": False, "text": "", "error": str(exc)}
