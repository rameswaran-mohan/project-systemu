#!/usr/bin/env python3
"""Render a URL headless with Playwright and capture a screenshot."""
from __future__ import annotations

import tempfile
from pathlib import Path

TOOL_META = {
    "name": "web_screenshot",
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
    selector: str = kwargs.get("selector", "")
    output_path: str = kwargs.get("output_path", "")

    if not url:
        return {"success": False, "image_path": "", "error": "url is required"}

    try:
        from playwright.sync_api import sync_playwright

        if not output_path:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            output_path = tmp.name
            tmp.close()
        else:
            output_path = str(Path(output_path).expanduser())
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle")

            if selector:
                try:
                    element = page.query_selector(selector)
                    if element:
                        element.screenshot(path=output_path)
                    else:
                        page.screenshot(path=output_path, full_page=True)
                except Exception:
                    page.screenshot(path=output_path, full_page=True)
            else:
                page.screenshot(path=output_path, full_page=True)

            browser.close()

        return {"success": True, "image_path": output_path, "error": None}

    except Exception as exc:
        if _is_playwright_missing(exc):
            return {
                "success": False,
                "image_path": "",
                "error": str(exc),
                "error_type": "missing_dependency",
                "fix": "Run: playwright install chromium",
            }
        return {"success": False, "image_path": "", "error": str(exc)}
