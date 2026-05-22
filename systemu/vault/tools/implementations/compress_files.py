#!/usr/bin/env python3
"""Compress files into a ZIP archive."""
from __future__ import annotations

import zipfile
from pathlib import Path

TOOL_META = {
    "name": "compress_files",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    output_path: str = kwargs.get("output_path", "")
    files: list = kwargs.get("files", []) or []
    include_dir: str = kwargs.get("include_dir", "") or ""

    if not output_path:
        return {"success": False, "output_path": "", "file_count": 0, "error": "output_path is required"}

    try:
        out = Path(output_path).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)

        all_files: list[Path] = []

        for f in files:
            p = Path(f).expanduser()
            if p.exists() and p.is_file():
                all_files.append(p)

        if include_dir:
            dir_path = Path(include_dir).expanduser()
            if dir_path.exists() and dir_path.is_dir():
                all_files.extend(p for p in dir_path.rglob("*") if p.is_file())

        file_count = 0
        with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in all_files:
                zf.write(str(fp), arcname=fp.name)
                file_count += 1

        return {"success": True, "output_path": str(out), "file_count": file_count, "error": None}

    except Exception as exc:
        return {"success": False, "output_path": "", "file_count": 0, "error": str(exc)}
