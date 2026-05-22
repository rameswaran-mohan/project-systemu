#!/usr/bin/env python3
"""Read a file and return its text content."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "file_read",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    path: str = kwargs.get("path", "")
    encoding: str = kwargs.get("encoding", "utf-8")

    if not path:
        return {"success": False, "content": "", "size_bytes": 0, "error": "path is required"}

    try:
        p = Path(path).expanduser()

        if not p.exists():
            return {"success": False, "content": "", "size_bytes": 0, "error": f"File not found: {p}"}

        if not p.is_file():
            return {"success": False, "content": "", "size_bytes": 0, "error": f"Path is not a file: {p}"}

        content = p.read_text(encoding=encoding)
        size_bytes = p.stat().st_size

        return {"success": True, "content": content, "size_bytes": size_bytes, "error": None}

    except Exception as exc:
        return {"success": False, "content": "", "size_bytes": 0, "error": str(exc)}
