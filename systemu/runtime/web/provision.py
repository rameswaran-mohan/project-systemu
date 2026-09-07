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


#: The three answers the chromium probe can give. UNKNOWN is the one this
#: module used to spell as ABSENT, which is D3: a probe that could not RUN was
#: read as a probe that had answered "no", and answered it again on every
#: single daemon start.
CHROMIUM_PRESENT = "PRESENT"
CHROMIUM_ABSENT = "ABSENT"
CHROMIUM_UNKNOWN = "UNKNOWN"

#: An exception message is written by somebody else. Capped so one runaway
#: repr cannot push the actionable half of the notice off the operator's
#: console, and capped AFTER the ASCII spelling so the cap counts the
#: characters that are actually printed (DEC-31 ordering).
_REASON_CAP = 200


class ChromiumVerdict:
    """PRESENT / ABSENT / UNKNOWN(reason), as one value.

    DEC-32 in its ordinary, non-security form: the fail-closed answer has to
    ride IN the value that crosses into the deciding frame. A bool cannot
    carry three states, so `chromium_present()` -- which is what this module
    used to decide on -- could not tell "playwright says no" apart from
    "playwright could not be asked", and spent 150 MB of the operator's
    connection on the difference.
    """

    __slots__ = ("state", "reason")

    def __init__(self, state: str, reason: str = "") -> None:
        self.state = state
        self.reason = reason

    def __repr__(self) -> str:                       # pragma: no cover - debug
        return "ChromiumVerdict(state={s!r}, reason={r!r})".format(
            s=self.state, r=self.reason)


def _chromium_executable_path():
    """Ask playwright where its chromium binary is. THE SYNC-API SEAM.

    RAISES whatever the sync API raises -- and that is the fix. This function
    used to wrap the whole call in `except Exception: return None`, so an
    `ImportError: DLL load failed while importing _greenlet` (witnessed on a
    scratch Windows install with chromium fully on disk), a missing VC
    runtime, or a half-finished install all came back as the same `None` that
    a genuinely absent browser produces. `probe_chromium` catches this, in the
    one frame that can tell the two apart.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return p.chromium.executable_path


def chromium_present() -> bool:
    """PRESENT, as a bool, for callers that only ever wanted the happy answer.

    RAISES when the probe cannot run. Nothing that DECIDES may call this:
    a decision needs `probe_chromium()`, because "no" and "could not tell"
    are different answers and only one of them may start a download.
    """
    path = _chromium_executable_path()
    if type(path) is not str or not path:
        return False
    return True if os.path.exists(path) else False


def _probe_reason(exc) -> str:
    """One short ASCII clause naming why the probe could not answer.

    The class name is kept: `ImportError` is the difference between "your
    browser is missing" and "this interpreter cannot load playwright", and it
    is the half an operator can search for.
    """
    name = type(exc).__name__
    text = _ascii("{n}: {m}".format(n=name, m=exc))
    if len(text) > _REASON_CAP:
        text = text[:_REASON_CAP] + "..."
    return text


def probe_chromium() -> ChromiumVerdict:
    """Is Playwright's chromium installed? PRESENT / ABSENT / UNKNOWN(reason).

    `chromium_present` is called through the module attribute on purpose: it
    is the seam this module's tests have always replaced, and it stays the one
    place the two ANSWERABLE states are decided. What is new is that its
    failure is caught HERE and given its own state, instead of being spelled
    as absence somewhere further down.
    """
    try:
        present = chromium_present()
    except Exception as exc:
        reason = _probe_reason(exc)
        logger.warning("[provision] the chromium probe could not run: %s",
                       reason, exc_info=True)
        return ChromiumVerdict(CHROMIUM_UNKNOWN, reason)
    if present is True:
        return ChromiumVerdict(CHROMIUM_PRESENT)
    return ChromiumVerdict(CHROMIUM_ABSENT)


def _state_of(verdict) -> str:
    """The state a decision may act on -- UNKNOWN for anything unrecognised.

    DEC-36: the concrete type is pinned in the frame that decides, with
    `type(x) is T`, and every other shape falls to UNKNOWN. Failing towards
    UNKNOWN is what keeps a stand-in from authorising a download; failing
    towards ABSENT is the defect being fixed.
    """
    if type(verdict) is not ChromiumVerdict:
        return CHROMIUM_UNKNOWN
    state = verdict.state
    if state is CHROMIUM_PRESENT:
        return CHROMIUM_PRESENT
    if state is CHROMIUM_ABSENT:
        return CHROMIUM_ABSENT
    return CHROMIUM_UNKNOWN


def _reason_of(verdict) -> str:
    if type(verdict) is not ChromiumVerdict:
        return "the probe returned no verdict"
    reason = verdict.reason
    return reason if type(reason) is str and reason else "no reason was given"


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


def chromium_probe_unknown_notice(reason) -> str:
    """The line printed when the probe could not run at all.

    It says three things and claims nothing else: that the question was not
    answered, that nothing is being downloaded on the strength of a
    non-answer, and that the browser tools stay off until the probe works. No
    size, because nothing is coming down the connection; no remedy command,
    because this module does not know which of the several causes it is and a
    guessed remedy is the defect one floor up.
    """
    return ("systemu: could not determine whether Chromium is installed "
            "({reason}); not downloading it; the web tools that need a browser "
            "stay unavailable until the probe can run.".format(
                reason=_ascii(reason)))


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
        # BEFORE the chromium probe, which asks playwright about its browser --
        # a question that has no answer when playwright is not there, and which
        # this module used to let decide whether to promise a download.
        line = playwright_missing_notice()
    else:
        verdict = probe_chromium()
        state = _state_of(verdict)
        if state is CHROMIUM_PRESENT:
            return None
        if state is CHROMIUM_UNKNOWN:
            # D3: an unanswerable probe is NOT an absent browser. Announcing a
            # download here is how a fully installed chromium was re-downloaded
            # on every start.
            line = chromium_probe_unknown_notice(_reason_of(verdict))
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
    verdict = probe_chromium()
    state = _state_of(verdict)
    if state is CHROMIUM_PRESENT:
        _bootstrapped = True
        return
    if state is CHROMIUM_UNKNOWN:
        # D3 -- THE DEFECT. `_chromium_executable_path` used to swallow the
        # sync API's exception and return None, and None was read as "not
        # installed". On a box where the sync API raises (witnessed:
        # `ImportError: DLL load failed while importing _greenlet`) that made
        # every daemon start announce and re-spawn a 150 MB download for a
        # chromium that was already on disk. An UNKNOWN authorises neither.
        _bootstrapped = True
        reason = _reason_of(verdict)
        logger.warning("[provision] chromium presence is unknown (%s) -- "
                       "no download", reason)
        say(chromium_probe_unknown_notice(reason))
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
