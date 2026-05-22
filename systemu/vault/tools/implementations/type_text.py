#!/usr/bin/env python3
"""type_text — Type a string of text into the currently focused window.

Works on any focused window — browser address bar, Word document,
Save As dialog, search fields, etc. OS-level key events via pynput.

Parameters (via run() kwargs):
  text  (str, required): The text to type.
  delay (float, optional): Seconds between keystrokes (default 0.02).
                           Increase for slow applications.

Returns (dict):
  success (bool): True if all characters were typed without error.
  error   (str|None): Error message on failure, otherwise None.
"""
from __future__ import annotations

import time

TOOL_META = {
    "name": "type_text",
    "tool_type": "desktop_action",
    "dependencies": ["pynput"],
}


def run(text: str, delay: float = 0.02) -> dict:
    """Type text into the focused window using pynput OS-level key events."""
    if not isinstance(text, str) or text == "":
        return {"success": False, "error": "text parameter is required and must be a non-empty string"}

    try:
        from pynput.keyboard import Controller

        keyboard = Controller()
        for char in text:
            keyboard.type(char)
            if delay > 0:
                time.sleep(delay)

        return {"success": True, "error": None}

    except Exception as exc:
        return {"success": False, "error": str(exc)}
