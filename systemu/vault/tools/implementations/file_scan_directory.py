#!/usr/bin/env python3
"""file_scan_directory — Recursively scan a directory and collect file metadata.

Parameters (via run() kwargs):
  source_path (str, required): Absolute or relative path to the directory to scan.

Returns (dict):
  success (bool): True if scan succeeded.
  files (list): List of dicts with name, extension, size_bytes, modified_at, relative_path.
  error (str|None): Error message or None.
"""
from __future__ import annotations
from pathlib import Path
from datetime import datetime

TOOL_META = {
    "name": "file_scan_directory",
    "tool_type": "python_function",
    "dependencies": [],
}


def run(source_path: str) -> dict:
    """Recursively scan source_path and collect file metadata."""
    if not source_path:
        return {"success": False, "files": [], "error": "source_path is required"}

    try:
        base = Path(source_path).resolve()
        if not base.is_dir():
            return {"success": False, "files": [], "error": f"source_path is not a directory: {source_path}"}

        files = []
        for entry in base.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    stat = entry.stat()
                    rel_path = str(entry.relative_to(base))
                    files.append({
                        "name": entry.name,
                        "extension": entry.suffix.lower(),
                        "size_bytes": stat.st_size,
                        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "relative_path": rel_path,
                    })
            except PermissionError:
                continue

        return {"success": True, "files": files, "error": None}

    except PermissionError:
        return {"success": False, "files": [], "error": f"Permission denied accessing {source_path}"}
    except Exception as exc:
        return {"success": False, "files": [], "error": str(exc)}
