#!/usr/bin/env python3
"""Search the web. Multi-provider: keyed (Brave/Serper) preferred, free DDG-lite fallback."""
from __future__ import annotations

TOOL_META = {"name": "web_search", "tool_type": "web", "dependencies": []}


def run(**kwargs) -> dict:
    query = kwargs.get("query", "")
    max_results = int(kwargs.get("max_results", 5))
    if not query:
        return {"success": False, "results": [], "error": "query is required", "missing_packages": []}
    try:
        from systemu.runtime.web.search_providers import search
        out = search(query, max_results)
        return {"success": bool(out["results"]), "results": out["results"],
                "provider": out.get("provider"), "degraded": out.get("degraded", False),
                "error": out.get("error")}
    except Exception as exc:
        return {"success": False, "results": [], "error": str(exc), "missing_packages": []}
