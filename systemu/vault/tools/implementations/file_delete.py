#!/usr/bin/env python3
"""Delete a file from the filesystem."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "file_delete",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    path: str = kwargs.get("path", "")

    if not path:
        return {"success": False, "error": "path is required"}

    try:
        p = Path(path).expanduser()

        if not p.exists():
            return {"success": False, "error": f"File not found: {p}"}

        if not p.is_file():
            return {"success": False, "error": f"Path is not a file: {p}"}

        p.unlink()
        return {"success": True, "error": None}

    except Exception as exc:
        return {"success": False, "error": str(exc)}
