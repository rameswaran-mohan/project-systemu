#!/usr/bin/env python3
"""keyboard_shortcut — Press a keyboard shortcut at the OS level.

Works on any focused window — browser, Word, Snipping Tool, etc.
Does NOT require a browser session.

Parameters (via run() kwargs):
  shortcut (str, required): Shortcut string, e.g. "Ctrl+V", "Ctrl+S",
                            "Alt+F4", "Win+D", "F5", "Enter".
                            Case-insensitive. "+" separates modifiers from key.

Returns (dict):
  success (bool): True if the shortcut was sent without error.
  error   (str|None): Error message on failure, otherwise None.
"""
from __future__ import annotations

import time

TOOL_META = {
    "name": "keyboard_shortcut",
    "tool_type": "desktop_action",
    "dependencies": ["pynput"],
}

# Modifier and special-key name → pynput Key
_KEY_MAP = {
    "ctrl":      "ctrl",
    "control":   "ctrl",
    "alt":       "alt",
    "shift":     "shift",
    "win":       "cmd",
    "windows":   "cmd",
    "super":     "cmd",
    "cmd":       "cmd",
    "enter":     "enter",
    "return":    "enter",
    "esc":       "esc",
    "escape":    "esc",
    "tab":       "tab",
    "backspace": "backspace",
    "delete":    "delete",
    "del":       "delete",
    "home":      "home",
    "end":       "end",
    "pageup":    "page_up",
    "page_up":   "page_up",
    "pagedown":  "page_down",
    "page_down": "page_down",
    "up":        "up",
    "down":      "down",
    "left":      "left",
    "right":     "right",
    "space":     "space",
    "f1":  "f1",  "f2":  "f2",  "f3":  "f3",  "f4":  "f4",
    "f5":  "f5",  "f6":  "f6",  "f7":  "f7",  "f8":  "f8",
    "f9":  "f9",  "f10": "f10", "f11": "f11", "f12": "f12",
}

_MODIFIERS = {"ctrl", "alt", "shift", "cmd"}


def run(shortcut: str) -> dict:
    """Send a keyboard shortcut to the focused window using pynput."""
    if not shortcut:
        return {"success": False, "error": "shortcut parameter is required"}

    try:
        from pynput.keyboard import Key, Controller

        keyboard = Controller()
        parts = [p.strip().lower() for p in shortcut.replace("+", "+").split("+") if p.strip()]

        resolved = []
        for part in parts:
            mapped = _KEY_MAP.get(part)
            if mapped:
                resolved.append(getattr(Key, mapped))
            elif len(part) == 1:
                resolved.append(part)
            else:
                return {"success": False, "error": f"Unknown key: '{part}' in shortcut '{shortcut}'"}

        # Press all keys in sequence, then release in reverse
        for key in resolved:
            keyboard.press(key)
            time.sleep(0.05)
        time.sleep(0.05)
        for key in reversed(resolved):
            keyboard.release(key)
            time.sleep(0.03)

        return {"success": True, "error": None}

    except Exception as exc:
        return {"success": False, "error": str(exc)}
