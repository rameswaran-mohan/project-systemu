#!/usr/bin/env python3
"""Search the web. Chain: Tavily/Exa/Brave/Serper (keyed) → ddgs (keyless free floor)."""
from __future__ import annotations

import os

TOOL_META = {"name": "web_search", "tool_type": "web", "dependencies": []}

# v0.9.8 Phase 1 Task 7: gate the keyless web_access stack on the env var so we
# don't depend on a Config object. Default ON.
_V2 = os.getenv("SYSTEMU_WEB_STACK_V2", "true").lower() != "false"

_NO_RESULTS_NOTE = (
    "Web search returned no usable results (search backend "
    "unavailable or query yielded nothing). Do NOT retry the same "
    "query repeatedly; if web data is essential and unavailable, FAIL "
    "the task with that reason.")


def run(**kwargs) -> dict:
    query = kwargs.get("query", "")
    max_results = int(kwargs.get("max_results", 5))
    if not query:
        return {"success": False, "results": [], "error": "query is required", "missing_packages": []}

    # v0.9.8 Phase 1 Task 7: under SYSTEMU_WEB_STACK_V2 (default on), delegate to
    # the keyless web_access layer (Jina-on-DuckDuckGo → raw DDG-lite fallback).
    # Map its {results, provider, error} into the existing tool return shape.
    # Legacy keyed/ddgs provider chain is preserved verbatim under `else`.
    if os.getenv("SYSTEMU_WEB_STACK_V2", "true").lower() != "false":
        try:
            from systemu.runtime import web_access
            out = web_access.search_web(query, max_results=max_results)
            results = out.get("results") or []
            resp = {"success": bool(results), "results": results,
                    "provider": out.get("provider"),
                    "degraded": True,  # keyless free floor
                    "error": out.get("error")}
            if not results:
                resp["note"] = _NO_RESULTS_NOTE
            return resp
        except Exception as exc:
            return {"success": False, "results": [], "error": str(exc), "missing_packages": []}

    try:
        from systemu.runtime.web.search_providers import search
        out = search(query, max_results)
        results = out["results"]
        degraded = out.get("degraded", False)
        resp = {"success": bool(results), "results": results,
                "provider": out.get("provider"), "degraded": degraded,
                "error": out.get("error")}
        if not results:
            resp["note"] = _NO_RESULTS_NOTE
        return resp
    except Exception as exc:
        return {"success": False, "results": [], "error": str(exc), "missing_packages": []}
