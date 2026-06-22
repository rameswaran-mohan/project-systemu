#!/usr/bin/env python3
"""Structured local business / point-of-interest lookup near a location.

v0.9.8 Phase 1 Task 7 — delegates to the keyless web_access layer's find_places,
which geocodes via OSM Nominatim and pulls named local POIs from Overpass. ODbL-
attributed. Use for "X shops/gyms/restaurants near me" style queries that need
structured results (names, addresses, opening hours) rather than web pages.
"""
from __future__ import annotations

TOOL_META = {"name": "find_places", "tool_type": "web", "dependencies": []}


def run(**kwargs) -> dict:
    query = kwargs.get("query", "") or ""
    if not query:
        return {"success": False, "places": [], "error": "query is required",
                "attribution": ""}

    near = kwargs.get("near")
    lat = kwargs.get("lat")
    lon = kwargs.get("lon")
    limit = int(kwargs.get("limit", 10))

    # Coerce lat/lon if passed as strings by the caller.
    def _f(v):
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    lat = _f(lat)
    lon = _f(lon)

    try:
        from systemu.runtime import web_access
        out = web_access.find_places(query, near=near, lat=lat, lon=lon, limit=limit)
        places = out.get("places") or []
        return {
            "success": bool(places),
            "places": places,
            "attribution": out.get("attribution", ""),
            "center": out.get("center"),
            "query": out.get("query", query),
            "error": out.get("error"),
        }
    except Exception as exc:
        return {"success": False, "places": [], "error": str(exc), "attribution": ""}
