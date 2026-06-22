#!/usr/bin/env python3
"""write_markdown_file — Write content to a markdown file at a specified path.

Parameters (via run() kwargs):
  file_path (str, required): The full path where the markdown file should be saved.
  content (str, required): The markdown formatted string to write.

Returns (dict):
  success (bool): True if the operation succeeded.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
import os
from pathlib import Path

TOOL_META = {
    "name": "write_markdown_file",
    "tool_type": "file_operation",
    "dependencies": [],
}


def run(file_path: str, content: str) -> dict:
    """Write content to a markdown file at a specified path."""
    if not file_path:
        return {"success": False, "error": "file_path is required"}
    if content is None:
        return {"success": False, "error": "content is required"}

    try:
        path = Path(file_path)
        os.makedirs(path.parent, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return {"success": True, "error": None}
    except (IOError, OSError) as exc:
        return {"success": False, "error": f"File operation failed: {str(exc)}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
