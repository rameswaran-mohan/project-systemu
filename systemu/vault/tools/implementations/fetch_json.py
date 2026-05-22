#!/usr/bin/env python3
"""HTTP GET a JSON endpoint and return the parsed response."""
from __future__ import annotations

TOOL_META = {
    "name": "fetch_json",
    "tool_type": "web",
    "dependencies": ["requests"],
}


def run(**kwargs) -> dict:
    url: str = kwargs.get("url", "")
    headers: dict = kwargs.get("headers", {}) or {}
    params: dict = kwargs.get("params", {}) or {}

    if not url:
        return {"success": False, "data": None, "status_code": 0, "error": "url is required"}

    try:
        import requests

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        return {"success": True, "data": data, "status_code": response.status_code, "error": None}

    except Exception as exc:
        status_code = 0
        try:
            status_code = exc.response.status_code  # type: ignore[attr-defined]
        except Exception:
            pass
        return {"success": False, "data": None, "status_code": status_code, "error": str(exc)}
