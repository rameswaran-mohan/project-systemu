#!/usr/bin/env python3
"""Download a file from a URL to a local path using streaming."""
from __future__ import annotations

from pathlib import Path

TOOL_META = {
    "name": "download_file",
    "tool_type": "file",
    "dependencies": ["requests"],
}


def run(**kwargs) -> dict:
    url: str = kwargs.get("url", "")
    output_path: str = kwargs.get("output_path", "")
    overwrite: bool = bool(kwargs.get("overwrite", False))

    if not url:
        return {"success": False, "output_path": "", "size_bytes": 0, "error": "url is required"}
    if not output_path:
        return {"success": False, "output_path": "", "size_bytes": 0, "error": "output_path is required"}

    try:
        import requests

        dest = Path(output_path).expanduser()

        if dest.exists() and not overwrite:
            return {"success": False, "output_path": str(dest), "size_bytes": 0, "error": "File already exists"}

        dest.parent.mkdir(parents=True, exist_ok=True)

        size = 0
        with requests.get(url, stream=True, timeout=30) as response:
            response.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        size += len(chunk)

        return {"success": True, "output_path": str(dest), "size_bytes": size, "error": None}

    except Exception as exc:
        return {"success": False, "output_path": "", "size_bytes": 0, "error": str(exc)}
