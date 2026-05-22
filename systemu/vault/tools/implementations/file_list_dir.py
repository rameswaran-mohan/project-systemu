#!/usr/bin/env python3
"""List files in a directory, optionally filtered by a glob pattern."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "file_list_dir",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    path: str = kwargs.get("path", "")
    pattern: str = kwargs.get("pattern", "*")
    recursive: bool = bool(kwargs.get("recursive", False))

    if not path:
        return {"success": False, "files": [], "count": 0, "error": "path is required"}

    if not pattern:
        pattern = "*"

    try:
        p = Path(path).expanduser()

        if not p.exists():
            return {"success": False, "files": [], "count": 0, "error": f"Directory not found: {p}"}

        if not p.is_dir():
            return {"success": False, "files": [], "count": 0, "error": f"Path is not a directory: {p}"}

        if recursive:
            matches = p.rglob(pattern)
        else:
            matches = p.glob(pattern)

        files = [str(f) for f in matches if f.is_file()]
        files.sort()

        return {"success": True, "files": files, "count": len(files), "error": None}

    except Exception as exc:
        return {"success": False, "files": [], "count": 0, "error": str(exc)}
