#!/usr/bin/env python3
"""Search the web. Chain: Tavily/Exa/Brave/Serper (keyed) → ddgs (keyless free floor)."""
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
        results = out["results"]
        degraded = out.get("degraded", False)
        resp = {"success": bool(results), "results": results,
                "provider": out.get("provider"), "degraded": degraded,
                "error": out.get("error")}
        if not results:
            resp["note"] = ("Web search returned no usable results (search backend "
                            "unavailable or query yielded nothing). Do NOT retry the same "
                            "query repeatedly; if web data is essential and unavailable, FAIL "
                            "the task with that reason.")
        return resp
    except Exception as exc:
        return {"success": False, "results": [], "error": str(exc), "missing_packages": []}
