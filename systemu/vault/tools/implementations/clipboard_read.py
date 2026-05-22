#!/usr/bin/env python3
"""Read the current text content from the system clipboard."""
from __future__ import annotations

TOOL_META = {
    "name": "clipboard_read",
    "tool_type": "system",
    "dependencies": ["pyperclip"],
}


def run(**kwargs) -> dict:
    try:
        import pyperclip

        text = pyperclip.paste()
        return {"success": True, "text": text, "error": None}

    except Exception as exc:
        return {"success": False, "text": "", "error": str(exc)}
