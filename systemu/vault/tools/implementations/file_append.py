#!/usr/bin/env python3
"""Append text to a file, creating it if it does not exist."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "file_append",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    path: str = kwargs.get("path", "")
    content: str = kwargs.get("content", "")
    encoding: str = kwargs.get("encoding", "utf-8")

    if not path:
        return {"success": False, "path": "", "error": "path is required"}

    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)

        with p.open("a", encoding=encoding) as f:
            f.write(content)

        return {"success": True, "path": str(p), "error": None}

    except Exception as exc:
        return {"success": False, "path": "", "error": str(exc)}
