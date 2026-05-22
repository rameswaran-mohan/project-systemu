#!/usr/bin/env python3
"""Write text to the system clipboard."""
from __future__ import annotations

TOOL_META = {
    "name": "clipboard_write",
    "tool_type": "system",
    "dependencies": ["pyperclip"],
}


def run(**kwargs) -> dict:
    text: str = kwargs.get("text", "")

    try:
        import pyperclip

        pyperclip.copy(text)
        return {"success": True, "error": None}

    except Exception as exc:
        return {"success": False, "error": str(exc)}
