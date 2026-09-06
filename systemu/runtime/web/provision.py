"""Chromium auto-provision + capability probe (v0.8.10).

On daemon start, if the Playwright chromium binary is missing, spawn a
background `playwright install chromium`. T0/T1 work immediately during the
install, so the operator is never fully blocked. Idempotent.

D10 -- the download is ANNOUNCED before it starts. It used to be disclosed only
through `logger.info`, which at the CLI's default WARNING level reaches nobody,
so `systemu daemon start` began pulling roughly 150 MB over the operator's
connection with nothing on their console saying so. Every line here is plain
ASCII (DEC-32c): a console that cannot encode the notice is a console the
operator reads nothing on."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

logger = logging.getLogger(__name__)

_bootstrapped = False

#: The one env var that turns the background download off. It is named VERBATIM
#: in the operator-facing lines: a remedy the operator has to go and look up is
#: not a remedy.
SKIP_AUTOINSTALL_ENV = "SYSTEMU_SKIP_BROWSER_AUTOINSTALL"

#: Deliberately approximate, and said as an approximation. Playwright's chromium
#: bundle moves with every release; a precise number here would be a precise
#: lie, and the operator only needs to know the order of magnitude before it
#: comes down their connection.
_DOWNLOAD_SIZE = "roughly 150 MB"

#: Named the same way in both notices so an operator who skips the download can
#: tell exactly which capabilities they are declining.
_WHAT_IT_BUYS = "screenshots, JavaScript-rendered pages, page interaction"

#: What the operator runs to do it later, by hand.
_MANUAL_COMMAND = "python -m playwright install chromium"


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


def opted_out() -> bool:
    """True when the operator has turned the background download off."""
    value = os.environ.get(SKIP_AUTOINSTALL_ENV)
    text = value if type(value) is str else ""
    return text.strip().lower() == "true"


def autoinstall_notice() -> str:
    """The line printed BEFORE the download is spawned.

    Names what is being downloaded, roughly how big it is, what it is for, that
    it happens in the background, and the exact variable that turns it off.
    """
    return ("systemu: Playwright's Chromium is not installed -- downloading it "
            "in the background now ({size}) so the web tools ({what}) can work. "
            "The daemon keeps starting; nothing waits for it. "
            "Set {env}=true to skip this.".format(
                size=_DOWNLOAD_SIZE, what=_WHAT_IT_BUYS,
                env=SKIP_AUTOINSTALL_ENV))


def autoinstall_skipped_notice() -> str:
    """The line printed when the operator's opt-out suppresses the download."""
    return ("systemu: not downloading Playwright's Chromium ({env}=true). The "
            "web tools that need a browser ({what}) stay unavailable until you "
            "run: {cmd}".format(env=SKIP_AUTOINSTALL_ENV, what=_WHAT_IT_BUYS,
                                cmd=_MANUAL_COMMAND))


def autoinstall_failed_notice(error) -> str:
    """The line printed when the spawn we just announced did not happen.

    An announcement that turns out to be false is worse than no announcement:
    the operator would sit waiting for browser tools that were never coming.
    """
    text = error if type(error) is str else str(error)
    return ("systemu: could not start the Playwright Chromium download ({err}). "
            "The web tools that need a browser ({what}) stay unavailable until "
            "you run: {cmd}".format(
                err=text.encode("ascii", "backslashreplace").decode("ascii"),
                what=_WHAT_IT_BUYS, cmd=_MANUAL_COMMAND))


def _console_write(line: str) -> None:
    """One plain line on the operator's console.

    `logger.info` is what this module used to do, and at the CLI's default
    WARNING level it reaches nobody -- that IS the defect. stderr rather than
    stdout, because stdout may be a pipe the operator is parsing.
    """
    stream = sys.stderr
    if stream is None:          # pythonw and friends have no console at all
        return
    stream.write(line + "\n")
    stream.flush()


def announce_browser_autoinstall(*, console=None) -> Optional[str]:
    """Disclose, on the OPERATOR's console, the download a daemon start implies.

    Called from the PARENT process before it spawns the daemon child, because
    the child's stdout is redirected into `daemon.log`: the child's own copy of
    this line never reaches the console, and the console is where the operator
    is standing when the download begins.

    Returns the line that was printed, or None when there is nothing to
    disclose (chromium is already installed).
    """
    say = console if console is not None else _console_write
    if opted_out():
        line = autoinstall_skipped_notice()
    elif chromium_present():
        return None
    else:
        line = autoinstall_notice()
    say(line)
    return line


def ensure_chromium_async(*, console=None) -> None:
    """If chromium missing and not opted out, spawn a one-shot background install."""
    global _bootstrapped
    say = console if console is not None else _console_write
    if _bootstrapped:
        return
    if opted_out():
        logger.info("[provision] browser auto-install skipped (env opt-out)")
        say(autoinstall_skipped_notice())
        return
    if chromium_present():
        _bootstrapped = True
        return
    _bootstrapped = True
    try:
        logger.info("[provision] chromium missing -- spawning background install")
        # D10: the notice goes out BEFORE the spawn. Announcing a download only
        # after starting it is the same silence with extra steps -- by then the
        # bytes are already moving.
        say(autoinstall_notice())
        subprocess.Popen(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _publish_banner()
    except Exception as exc:
        logger.exception("[provision] failed to spawn chromium install")
        # We already said it was starting. Retract it in the same place.
        try:
            say(autoinstall_failed_notice(exc))
        except Exception:
            logger.debug("[provision] failure notice write failed", exc_info=True)


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
