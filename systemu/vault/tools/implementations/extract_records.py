#!/usr/bin/env python3
"""Extract structured records from HTML/text the caller has already fetched.

v0.8.21 — use this AFTER web_read / fetch_html when you have content in hand.
If you only have a URL, use `web_extract` instead — do NOT chain web_read +
extract_records for URL-start cases."""
from __future__ import annotations

TOOL_META = {
    "name": "extract_records",
    "tool_type": "api_call",
    "dependencies": ["jsonschema"],
}


def run(**kwargs) -> dict:
    text: str = kwargs.get("text", "") or ""
    schema = kwargs.get("schema") or {}
    max_records: int = int(kwargs.get("max_records", 20))
    from systemu.runtime.extractor import extract_records
    return extract_records(text, schema, max_records=max_records)
