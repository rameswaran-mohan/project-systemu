#!/usr/bin/env python3
"""Parse a JSON string or file path and return the Python data structure."""
from __future__ import annotations

import json
from pathlib import Path

TOOL_META = {
    "name": "parse_json",
    "tool_type": "utility",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    input_val: str = kwargs.get("input", "")
    mode: str = kwargs.get("mode", "auto") or "auto"

    if not input_val and input_val != "":
        return {"success": False, "data": None, "error": "input is required"}

    try:
        if mode == "string":
            data = json.loads(input_val)
            return {"success": True, "data": data, "error": None}

        if mode == "file":
            p = Path(input_val).expanduser()
            if not p.exists():
                return {"success": False, "data": None, "error": f"File not found: {p}"}
            data = json.loads(p.read_text(encoding="utf-8"))
            return {"success": True, "data": data, "error": None}

        # mode == "auto": try JSON string first, then file
        try:
            data = json.loads(input_val)
            return {"success": True, "data": data, "error": None}
        except json.JSONDecodeError:
            pass

        p = Path(input_val).expanduser()
        if p.exists() and p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            return {"success": True, "data": data, "error": None}

        return {"success": False, "data": None, "error": "Input is not valid JSON and not a valid file path"}

    except Exception as exc:
        return {"success": False, "data": None, "error": str(exc)}
