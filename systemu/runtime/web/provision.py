"""Chromium auto-provision + capability probe (v0.8.10).

On daemon start, if the Playwright chromium binary is missing, spawn a
background `playwright install chromium`. T0/T1 work immediately during the
install, so the operator is never fully blocked. Idempotent.

D10 -- the download is ANNOUNCED before it starts. It used to be disclosed only
through `logger.info`, which at the CLI's default WARNING level reaches nobody,
so `systemu daemon start` began pulling roughly 150 MB over the operator's
connection with nothing on their console saying so. Every line here is plain
ASCII (DEC-32c): a console that cannot encode the notice is a console the
operator reads nothing on.

N2 -- the announcement is only made when playwright can ACTUALLY run it.
`pip install "systemu[dashboard]"` -- the command the docs give for the
dashboard -- does not pull `playwright`, which lives under the `browser` and
`all` extras. On exactly that install this module announced a 150 MB download,
spawned `python -m playwright install chromium` into DEVNULL, and the child
died instantly with `No module named playwright`. Popen does not raise for a
child that starts and then exits non-zero, so the retraction never fired: the
operator was told a download was under way that was never going to happen, with
no output kept anywhere to say otherwise. So, before anything is announced or
spawned, importability is probed; and an install that IS announced is watched
to its exit, with its output kept beside the daemon's own log."""
from __future__ import annotations

import datetime
import importlib.util
import logging
import os
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_bootstrapped = False

#: The thread watching the install we last spawned, or None. Module-level so a
#: test can join it: a waiter nothing can wait for is a waiter nothing witnesses.
_install_waiter = None

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

#: The step BEFORE that one on a `systemu[dashboard]` install, where the
#: playwright wheel is not there at all. Quoted because an unquoted
#: `systemu[browser]` is a glob to zsh.
_EXTRA_COMMAND = 'pip install "systemu[browser]"'

#: The child's output, kept beside `daemon.log` in the operating vault.
INSTALL_LOG_NAME = "browser_install.log"

#: The daemon's own log, and the only thing this module will sit beside.
_DAEMON_LOG_NAME = "daemon.log"


def playwright_importable() -> bool:
    """Can THIS interpreter import playwright at all?

    `find_spec`, not a try/import: importing playwright to find out whether it
    is importable costs the whole package at daemon start, on the hot path of a
    command the operator is waiting on.

    Fails CLOSED (DEC-32): any exception -- a broken meta path, a finder that
    raises -- is "no". A probe that cannot answer must not be what authorises a
    150 MB announcement.
    """
    try:
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        logger.debug("[provision] playwright importability probe failed",
                     exc_info=True)
        return False


def _remedy() -> str:
    """The steps left between here and a working browser tool, on THIS install.

    The wheel and the browser are two different absences and two different
    commands. Naming the browser step alone to an operator who has no
    playwright wheel is the defect this module was carrying: the remedy failed
    in exactly the way the announcement did.
    """
    if playwright_importable():
        return _MANUAL_COMMAND
    return "{extra} and then {cmd}".format(extra=_EXTRA_COMMAND,
                                           cmd=_MANUAL_COMMAND)


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
                                cmd=_remedy()))


def playwright_missing_notice() -> str:
    """The line printed INSTEAD of an announcement when there is no playwright.

    No size, because nothing is coming down the connection. The optional-deps
    registry already reports web_act and web_screenshot as unavailable and why;
    this does not restate that inventory, it names the one thing the operator
    can do about it, once, with both of its steps.
    """
    return ("systemu: Playwright is not installed, so there is no Chromium "
            "download to start. The web tools that need a browser ({what}) "
            "stay unavailable until you run: {cmd}".format(
                what=_WHAT_IT_BUYS, cmd=_remedy()))


def _ascii(text) -> str:
    """One ASCII spelling of a value that is about to be read on a console.

    DEC-32c: a cp1252 console cannot encode anything else, and at daemon start
    the failure would be a UnicodeEncodeError in place of the notice.
    """
    value = text if type(text) is str else str(text)
    return value.encode("ascii", "backslashreplace").decode("ascii")


def autoinstall_failed_notice(error, *, log_path=None) -> str:
    """The line printed when the download we just announced did not happen.

    An announcement that turns out to be false is worse than no announcement:
    the operator would sit waiting for browser tools that were never coming.
    `log_path`, when there is one, is the file the child's own words are in --
    without it the operator is told the install failed and given nothing to
    read about why.
    """
    detail = _ascii(error)
    if log_path is not None:
        detail = "{d}; its output is in {p}".format(d=detail,
                                                    p=_ascii(log_path))
    return ("systemu: could not start the Playwright Chromium download ({err}). "
            "The web tools that need a browser ({what}) stay unavailable until "
            "you run: {cmd}".format(err=detail, what=_WHAT_IT_BUYS,
                                    cmd=_remedy()))


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
    elif not playwright_importable():
        # BEFORE `chromium_present`, which asks playwright about its browser --
        # a question that has no answer when playwright is not there, and which
        # this module used to let decide whether to promise a download.
        line = playwright_missing_notice()
    elif chromium_present():
        return None
    else:
        line = autoinstall_notice()
    say(line)
    return line


def _install_log_path() -> Optional[str]:
    """Where the child's output goes: beside the daemon's own log.

    None when there is no daemon log to sit beside -- and then the spawn keeps
    no log at all rather than MINTING somewhere to put one. A background
    convenience must never be what creates the operating vault; a provision
    probe run from the wrong directory would quietly leave one behind.
    """
    try:
        from systemu.runtime.vault_root import resolve_vault_root

        verdict = resolve_vault_root()
        if verdict.refused:
            return None
        root = verdict.root
    except Exception:
        logger.debug("[provision] could not resolve the vault root",
                     exc_info=True)
        return None
    if type(root) is not str:
        return None
    if not os.path.isfile(os.path.join(root, _DAEMON_LOG_NAME)):
        return None
    return os.path.join(root, INSTALL_LOG_NAME)


def _open_install_log(path: str):
    """The child's stdout sink, or None if it cannot be opened.

    Binary append, so nothing in this process ever decodes the child's bytes:
    Popen hands the raw descriptor to the child, and a text wrapper here would
    only invent an encoding for output it never reads. The header is plain
    ASCII so the file explains itself when the install dies before writing a
    word of its own.
    """
    try:
        handle = open(path, "ab")
    except Exception:
        logger.debug("[provision] could not open %s", path, exc_info=True)
        return None
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handle.write("[provision] {stamp} -- {cmd}\n".format(
            stamp=stamp, cmd=_MANUAL_COMMAND).encode("ascii",
                                                     "backslashreplace"))
        handle.flush()
    except Exception:
        logger.debug("[provision] install log header write failed",
                     exc_info=True)
    return handle


def _close(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        logger.debug("[provision] install log close failed", exc_info=True)


def _watch_install(proc, handle, log_path, say) -> None:
    """Wait on the install we announced, and report an exit that failed.

    THE DEFECT this exists for: `Popen` returns happily for a child that starts
    and then dies a millisecond later, so the only way to learn that the
    announced download never happened is to wait for the code.

    Says nothing on success -- a retraction on every install would be a lie in
    the other direction -- and nothing when the code cannot be read at all,
    because "could not start" is a claim, and an unreadable exit is not
    evidence for it.
    """
    code = None
    try:
        code = proc.wait()
    except Exception:
        logger.debug("[provision] could not wait on the chromium install",
                     exc_info=True)
        return
    finally:
        _close(handle)
    if type(code) is int and code == 0:
        logger.info("[provision] chromium install finished")
        return
    logger.warning("[provision] chromium install exited %r", code)
    try:
        say(autoinstall_failed_notice("exit code {}".format(code),
                                      log_path=log_path))
    except Exception:
        logger.debug("[provision] failure notice write failed", exc_info=True)


def ensure_chromium_async(*, console=None) -> None:
    """If chromium missing and not opted out, spawn a one-shot background install."""
    global _bootstrapped, _install_waiter
    say = console if console is not None else _console_write
    if _bootstrapped:
        return
    if opted_out():
        logger.info("[provision] browser auto-install skipped (env opt-out)")
        say(autoinstall_skipped_notice())
        return
    if not playwright_importable():
        # `pip install "systemu[dashboard]"` lands here. Nothing to announce and
        # nothing to spawn: `python -m playwright install chromium` would die on
        # `No module named playwright` with its output in DEVNULL, leaving the
        # operator holding a promise of 150 MB that was never going to arrive.
        _bootstrapped = True
        logger.info("[provision] playwright is not installed -- "
                    "no chromium download to announce")
        say(playwright_missing_notice())
        return
    if chromium_present():
        _bootstrapped = True
        return
    _bootstrapped = True
    log_path = _install_log_path()
    handle = None if log_path is None else _open_install_log(log_path)
    if handle is None:
        log_path = None
    try:
        logger.info("[provision] chromium missing -- spawning background install")
        # D10: the notice goes out BEFORE the spawn. Announcing a download only
        # after starting it is the same silence with extra steps -- by then the
        # bytes are already moving.
        say(autoinstall_notice())
        proc = subprocess.Popen(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdout=(subprocess.DEVNULL if handle is None else handle),
            stderr=subprocess.STDOUT,
        )
        _publish_banner()
        _install_waiter = threading.Thread(
            target=_watch_install, args=(proc, handle, log_path, say),
            name="systemu-browser-install", daemon=True,
        )
        _install_waiter.start()
    except Exception as exc:
        _close(handle)
        logger.exception("[provision] failed to spawn chromium install")
        # We already said it was starting. Retract it in the same place.
        try:
            say(autoinstall_failed_notice(exc, log_path=log_path))
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
