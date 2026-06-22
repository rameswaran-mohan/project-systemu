#!/usr/bin/env python3
"""Write text content to a file, creating parent directories as needed."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "file_write",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    path: str = kwargs.get("path", "")
    content: str = kwargs.get("content", "")
    encoding: str = kwargs.get("encoding", "utf-8")
    overwrite: bool = bool(kwargs.get("overwrite", True))

    if not path:
        return {"success": False, "path": "", "size_bytes": 0, "error": "path is required"}

    try:
        p = Path(path).expanduser()

        if p.exists() and not overwrite:
            return {"success": False, "path": str(p), "size_bytes": 0, "error": "File already exists"}

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding)
        size_bytes = p.stat().st_size

        return {"success": True, "path": str(p), "size_bytes": size_bytes, "error": None}

    except Exception as exc:
        return {"success": False, "path": "", "size_bytes": 0, "error": str(exc)}
