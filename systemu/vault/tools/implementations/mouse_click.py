#!/usr/bin/env python3
"""mouse_click — Click a target element or screen coordinate.

Tries browser (Chrome CDP) first for selector-based clicks on web content.
Falls back to OS-level coordinate click via pynput for desktop UI targets.

Parameters (via run() kwargs):
  target  (str, required): CSS selector, visible text, or element description.
  x       (int, optional): Screen X coordinate — skips browser attempt if given.
  y       (int, optional): Screen Y coordinate — skips browser attempt if given.
  button  (str, optional): "left" | "right" | "middle"  (default "left").
  double  (bool, optional): True for double-click (default False).

Returns (dict):
  success (bool): True if the click succeeded.
  method  (str): "browser" or "screen" — which path was used.
  error   (str|None): Error message on failure, otherwise None.
"""
from __future__ import annotations

import time

TOOL_META = {
    "name": "mouse_click",
    "tool_type": "browser_action",
    "dependencies": ["playwright", "pynput"],
}


def run(
    target: str,
    x: int = None,
    y: int = None,
    button: str = "left",
    double: bool = False,
) -> dict:
    """Click a target using the best available method."""
    if not target and x is None:
        return {"success": False, "method": None, "error": "Provide target or x/y coordinates."}

    # ── Direct screen-coordinate click ────────────────────────────────────────
    if x is not None and y is not None:
        return _screen_click(int(x), int(y), button=button, double=double)

    # ── Browser (CDP) click — for web page elements ───────────────────────────
    browser_result = _browser_click(target, button=button, double=double)
    if browser_result["success"]:
        return browser_result

    # ── Screen click fallback — no coordinates, so we can't auto-position ─────
    # Return the browser error; the shadow should retry with explicit x,y.
    return browser_result


def _browser_click(target: str, *, button: str, double: bool) -> dict:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp("http://localhost:9222")
            except Exception:
                return {
                    "success": False,
                    "method": "browser",
                    "error": "No Chrome CDP session on localhost:9222. "
                             "Provide x,y coordinates for desktop UI clicks.",
                }

            contexts = browser.contexts
            if not contexts:
                return {"success": False, "method": "browser", "error": "No browser context available."}
            pages = contexts[0].pages
            if not pages:
                return {"success": False, "method": "browser", "error": "No open pages."}
            page = pages[0]

            btn = {"left": "left", "right": "right", "middle": "middle"}.get(button, "left")
            click_count = 2 if double else 1

            # Try CSS selector first, then visible text
            try:
                page.click(target, button=btn, click_count=click_count, timeout=4000)
                return {"success": True, "method": "browser", "error": None}
            except PWTimeout:
                pass

            try:
                page.get_by_text(target, exact=False).first.click(
                    button=btn, click_count=click_count, timeout=4000,
                )
                return {"success": True, "method": "browser", "error": None}
            except Exception as exc2:
                return {"success": False, "method": "browser", "error": str(exc2)}

    except Exception as exc:
        return {"success": False, "method": "browser", "error": str(exc)}


def _screen_click(x: int, y: int, *, button: str, double: bool) -> dict:
    try:
        from pynput.mouse import Button, Controller

        btn_map = {"left": Button.left, "right": Button.right, "middle": Button.middle}
        btn = btn_map.get(button, Button.left)

        mouse = Controller()
        mouse.position = (x, y)
        time.sleep(0.1)
        if double:
            mouse.click(btn, 2)
        else:
            mouse.click(btn)
        time.sleep(0.05)
        return {"success": True, "method": "screen", "error": None}

    except Exception as exc:
        return {"success": False, "method": "screen", "error": str(exc)}
