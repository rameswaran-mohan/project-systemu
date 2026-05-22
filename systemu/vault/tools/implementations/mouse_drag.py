#!/usr/bin/env python3
"""mouse_drag — Drag the mouse across the screen (OS-level).

Used for operations like drawing a selection rectangle in Snipping Tool,
resizing windows, or drag-and-drop on the desktop.

Parameters (via run() kwargs):
  start_x (int, required): Starting X screen coordinate.
  start_y (int, required): Starting Y screen coordinate.
  end_x   (int, required): Ending X screen coordinate.
  end_y   (int, required): Ending Y screen coordinate.
  target  (str, optional): Human-readable description (logged only).
  steps   (int, optional): Smoothness — number of intermediate move steps (default 30).

Returns (dict):
  success (bool): True if the drag completed without error.
  error   (str|None): Error message on failure, otherwise None.
"""
from __future__ import annotations

import time

TOOL_META = {
    "name": "mouse_drag",
    "tool_type": "desktop_action",
    "dependencies": ["pynput"],
}


def run(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    target: str = "",
    steps: int = 30,
) -> dict:
    """Perform a smooth mouse drag using pynput (OS-level screen coordinates)."""
    for name, val in [("start_x", start_x), ("start_y", start_y),
                      ("end_x", end_x), ("end_y", end_y)]:
        if val is None:
            return {"success": False, "error": f"Required parameter '{name}' is missing."}

    try:
        from pynput.mouse import Button, Controller

        mouse = Controller()

        # Move to start position and press
        mouse.position = (int(start_x), int(start_y))
        time.sleep(0.15)
        mouse.press(Button.left)
        time.sleep(0.05)

        # Smooth movement across steps
        dx = (int(end_x) - int(start_x)) / max(steps, 1)
        dy = (int(end_y) - int(start_y)) / max(steps, 1)
        for i in range(1, steps + 1):
            mouse.position = (int(start_x + dx * i), int(start_y + dy * i))
            time.sleep(0.01)

        # Release at end position
        time.sleep(0.05)
        mouse.release(Button.left)
        time.sleep(0.1)

        return {"success": True, "error": None}

    except Exception as exc:
        return {"success": False, "error": str(exc)}
