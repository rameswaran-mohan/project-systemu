#!/usr/bin/env python3
"""HTTP GET a URL and return the raw HTML response."""
from __future__ import annotations

TOOL_META = {
    "name": "fetch_html",
    "tool_type": "web",
    "dependencies": ["requests"],
}


def run(**kwargs) -> dict:
    url: str = kwargs.get("url", "")
    headers: dict = kwargs.get("headers", {}) or {}

    if not url:
        return {"success": False, "html": "", "status_code": 0, "error": "url is required"}

    try:
        import requests

        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return {"success": True, "html": response.text, "status_code": response.status_code, "error": None}

    except Exception as exc:
        status_code = 0
        try:
            status_code = exc.response.status_code  # type: ignore[attr-defined]
        except Exception:
            pass
        return {"success": False, "html": "", "status_code": status_code, "error": str(exc)}
