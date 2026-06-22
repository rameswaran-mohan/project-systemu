#!/usr/bin/env python3
"""Format a date string from one format to another using strptime/strftime."""
from __future__ import annotations

from datetime import datetime

TOOL_META = {
    "name": "format_date",
    "tool_type": "utility",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    date_str: str = kwargs.get("date_str", "")
    input_format: str = kwargs.get("input_format", "%Y-%m-%d") or "%Y-%m-%d"
    output_format: str = kwargs.get("output_format", "%m/%d/%Y") or "%m/%d/%Y"

    if not date_str:
        return {"success": False, "result": "", "error": "date_str is required"}

    try:
        dt = datetime.strptime(date_str, input_format)
        result = dt.strftime(output_format)
        return {"success": True, "result": result, "error": None}

    except Exception as exc:
        return {"success": False, "result": "", "error": str(exc)}
