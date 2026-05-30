"""Chromium auto-provision + capability probe (v0.8.10).

On daemon start, if the Playwright chromium binary is missing, spawn a
background `playwright install chromium`. T0/T1 work immediately during the
install, so the operator is never fully blocked. Idempotent."""
from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

_bootstrapped = False


def _chromium_executable_path():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            return path
    except Exception:
        return None


def chromium_present() -> bool:
    path = _chromium_executable_path()
    if not path:
        return False
    return os.path.exists(path)


def ensure_chromium_async() -> None:
    """If chromium missing and not opted out, spawn a one-shot background install."""
    global _bootstrapped
    if _bootstrapped:
        return
    if (os.environ.get("SYSTEMU_SKIP_BROWSER_AUTOINSTALL") or "").lower() == "true":
        logger.info("[provision] browser auto-install skipped (env opt-out)")
        return
    if chromium_present():
        _bootstrapped = True
        return
    _bootstrapped = True
    try:
        logger.info("[provision] chromium missing — spawning background install")
        subprocess.Popen(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _publish_banner()
    except Exception:
        logger.exception("[provision] failed to spawn chromium install")


def _publish_banner() -> None:
    try:
        from systemu.interface.event_bus import EventBus
        EventBus.get().publish({
            "category": "system", "level": "INFO",
            "message": "🌐 Setting up browser… web search & page reading work now; "
                       "full browsing (screenshots, JS pages, interaction) ready in ~30–60s.",
            "context": {"kind": "browser_provisioning"},
        })
    except Exception:
        logger.debug("[provision] banner publish failed", exc_info=True)
