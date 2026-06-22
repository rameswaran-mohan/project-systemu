#!/usr/bin/env python3
"""Copy a file from one location to another."""
from __future__ import annotations

import shutil
from pathlib import Path

TOOL_META = {
    "name": "file_copy",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    src: str = kwargs.get("src", "")
    dst: str = kwargs.get("dst", "")
    overwrite: bool = bool(kwargs.get("overwrite", False))

    if not src:
        return {"success": False, "dst": "", "error": "src is required"}
    if not dst:
        return {"success": False, "dst": "", "error": "dst is required"}

    try:
        src_path = Path(src).expanduser()
        dst_path = Path(dst).expanduser()

        if not src_path.exists():
            return {"success": False, "dst": "", "error": f"Source file not found: {src_path}"}

        if not src_path.is_file():
            return {"success": False, "dst": "", "error": f"Source is not a file: {src_path}"}

        if dst_path.exists() and not overwrite:
            return {"success": False, "dst": str(dst_path), "error": "Destination file already exists"}

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_path), str(dst_path))

        return {"success": True, "dst": str(dst_path), "error": None}

    except Exception as exc:
        return {"success": False, "dst": "", "error": str(exc)}
