"""T2 — hardened headless browser. Headless-only (no attach to operator
Chrome). Accessibility-tree-first interaction. Context pool with a
concurrency cap mirroring the v0.8.6 bounded-queue model."""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MAX_CONTEXTS = int(os.environ.get("SYSTEMU_BROWSER_MAX_CONTEXTS", "3"))


def _make_semaphore() -> threading.Semaphore:
    return threading.Semaphore(_MAX_CONTEXTS)


def _domains(env: str) -> List[str]:
    return [d.strip().lower() for d in (os.environ.get(env, "") or "").split(",") if d.strip()]


def is_url_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    deny = _domains("SYSTEMU_WEB_DENY_DOMAINS")
    allow = _domains("SYSTEMU_WEB_ALLOW_DOMAINS")
    if any(host == d or host.endswith("." + d) for d in deny):
        return False
    if allow:
        return any(host == d or host.endswith("." + d) for d in allow)
    return True


def parse_a11y_snapshot(raw: Dict[str, Any]) -> List[Dict[str, str]]:
    """Flatten a Playwright accessibility tree into interactive nodes."""
    out: List[Dict[str, str]] = []
    interactive = {"link", "button", "textbox", "checkbox", "combobox", "menuitem", "tab", "searchbox"}
    counter = {"n": 0}

    def walk(node):
        role = node.get("role", "")
        name = node.get("name", "")
        if role in interactive:
            counter["n"] += 1
            out.append({"role": role, "name": name, "ref": f"e{counter['n']}"})
        for ch in node.get("children", []) or []:
            walk(ch)
    walk(raw)
    return out


class BrowserPool:
    _instance: Optional["BrowserPool"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._sem = _make_semaphore()
        self._pw = None
        self._browser = None

    @classmethod
    def get(cls) -> "BrowserPool":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _ensure_browser(self):
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)

    def render_html(self, url: str, timeout_ms: int = 20000) -> str:
        if not is_url_allowed(url):
            raise PermissionError(f"URL blocked by domain policy: {url}")
        with self._sem:
            self._ensure_browser()
            ctx = self._browser.new_context()
            try:
                page = ctx.new_page()
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                return page.content()
            finally:
                ctx.close()

    def screenshot(self, url: str, output_path: str, timeout_ms: int = 20000) -> str:
        if not is_url_allowed(url):
            raise PermissionError(f"URL blocked by domain policy: {url}")
        with self._sem:
            self._ensure_browser()
            ctx = self._browser.new_context()
            try:
                page = ctx.new_page()
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                page.screenshot(path=output_path, full_page=True)
                return output_path
            finally:
                ctx.close()

    def teardown(self):
        try:
            if self._browser: self._browser.close()
            if self._pw: self._pw.stop()
        except Exception:
            pass
        self._browser = None; self._pw = None
