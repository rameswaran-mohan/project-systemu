#!/usr/bin/env python3
"""write_text_file — Write raw text content to a file at a specified path.

Parameters (via run() kwargs):
  file_path (str, required): Full path to the output file.
  content (str, required): The text content to write.

Returns (dict):
  success (bool): True if the operation succeeded.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
from pathlib import Path

TOOL_META = {
    "name": "write_text_file",
    "tool_type": "file_operation",
    "dependencies": [],
}


def run(file_path: str, content: str) -> dict:
    """Write text content to a file, creating parent directories if necessary."""
    if not file_path:
        return {"success": False, "error": "file_path is required"}
    if content is None:
        return {"success": False, "error": "content is required"}

    try:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"success": True, "error": None}
    except OSError as exc:
        return {"success": False, "error": f"File system error: {str(exc)}"}
    except Exception as exc:
        return {"success": False, "error": f"Unexpected error: {str(exc)}"}
