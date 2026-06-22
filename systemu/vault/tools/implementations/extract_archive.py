#!/usr/bin/env python3
"""Extract a ZIP or TAR archive to a specified output directory."""
from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

TOOL_META = {
    "name": "extract_archive",
    "tool_type": "file",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    archive_path: str = kwargs.get("archive_path", "")
    output_dir: str = kwargs.get("output_dir", "")

    if not archive_path:
        return {"success": False, "output_dir": "", "file_count": 0, "error": "archive_path is required"}
    if not output_dir:
        return {"success": False, "output_dir": "", "file_count": 0, "error": "output_dir is required"}

    try:
        src = Path(archive_path).expanduser()
        dst = Path(output_dir).expanduser()

        if not src.exists():
            return {"success": False, "output_dir": "", "file_count": 0, "error": f"Archive not found: {src}"}

        dst.mkdir(parents=True, exist_ok=True)

        name_lower = src.name.lower()
        file_count = 0

        if name_lower.endswith(".zip"):
            with zipfile.ZipFile(str(src), "r") as zf:
                members = zf.namelist()
                zf.extractall(str(dst))
                file_count = len([m for m in members if not m.endswith("/")])

        elif name_lower.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
            with tarfile.open(str(src), "r:*") as tf:
                members = tf.getmembers()
                tf.extractall(str(dst))
                file_count = len([m for m in members if m.isfile()])

        else:
            return {
                "success": False,
                "output_dir": "",
                "file_count": 0,
                "error": f"Unsupported archive format: {src.suffix}",
            }

        return {"success": True, "output_dir": str(dst), "file_count": file_count, "error": None}

    except Exception as exc:
        return {"success": False, "output_dir": "", "file_count": 0, "error": str(exc)}
