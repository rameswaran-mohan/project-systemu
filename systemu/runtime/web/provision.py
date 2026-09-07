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
import json
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

    `path` is the WITNESS, never the decision: the executable a PRESENT verdict
    is about, so the log line can name it. Empty on every other state, and
    empty when the path could not be re-read -- a missing witness downgrades
    the LINE, never the verdict.
    """

    __slots__ = ("state", "reason", "path")

    def __init__(self, state: str, reason: str = "", path: str = "") -> None:
        self.state = state
        self.reason = reason
        self.path = path

    def __repr__(self) -> str:                       # pragma: no cover - debug
        return "ChromiumVerdict(state={s!r}, reason={r!r}, path={p!r})".format(
            s=self.state, r=self.reason, p=self.path)


class ChromiumProbeError(Exception):
    """The probe could not be RUN. Not "the browser is missing".

    Everything that raises this ends as UNKNOWN(reason). It exists so the
    reason reaching the operator is a sentence about the registry rather than
    an `IndexError` from three frames down.
    """


#: Playwright's own browser registry, relative to the installed package. It is
#: the only thing that knows which chromium build THIS playwright will launch,
#: and it is a plain JSON file: reading it costs a file read, where asking the
#: driver the same question costs a Node process, a greenlet and an event loop.
_REGISTRY_RELPATH = ("driver", "package", "browsers.json")

#: The env var playwright honours for its browsers directory. Read here for the
#: same reason playwright reads it: an operator who moved the browsers has
#: moved the answer to "is chromium installed", and looking in the default
#: place would report a present browser absent and re-download it.
BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"

#: The directory playwright installs browsers into, under every platform cache.
_BROWSERS_DIRNAME = "ms-playwright"

#: Where playwright's documented `PLAYWRIGHT_BROWSERS_PATH=0` puts them --
#: inside the wheel itself, not in a relative directory named "0".
_LOCAL_BROWSERS_RELPATH = ("driver", "package", ".local-browsers")

#: The executable layout inside `chromium-<revision>/`, newest first, per
#: platform. `browsers.json` carries the revision but not the layout, so this
#: half is replicated from playwright's published directory names; it is a
#: CANDIDATE list and the first one that exists wins, which is what keeps a
#: layout rename (chrome-win -> chrome-win64) from reading as an absent
#: browser.
_CHROMIUM_EXECUTABLES = {
    "win32": (("chrome-win64", "chrome.exe"),
              ("chrome-win", "chrome.exe")),
    "darwin": (("chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
               ("chrome-mac-arm64", "Chromium.app", "Contents", "MacOS",
                "Chromium")),
    "linux": (("chrome-linux", "chrome"),),
}

#: The characters a build revision may be made of. The registry ships inside
#: the playwright wheel, so this is not an attacker boundary -- it is the
#: cheapest way to keep a corrupted line from being joined onto a path.
_REVISION_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz"
                            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")


def _playwright_package_dir() -> str:
    """Where the installed playwright package lives, WITHOUT importing it.

    `find_spec` locates a package; it does not execute one. That is the whole
    difference this finding turns on: importing `playwright.sync_api` pulls in
    greenlet, the Node driver and an event loop, and it was that loop's
    teardown -- not the browser, not the daemon -- that printed
    `Task was destroyed but it is pending!` and a `TargetClosedError` after a
    start that had already succeeded.
    """
    try:
        spec = importlib.util.find_spec("playwright")
    except Exception as exc:
        raise ChromiumProbeError(
            "playwright could not be located ({})".format(type(exc).__name__))
    if spec is None:
        raise ChromiumProbeError("playwright is not installed")
    locations = getattr(spec, "submodule_search_locations", None)
    try:
        entries = list(locations or ())
    except Exception:
        entries = []
    for entry in entries:
        if type(entry) is str and entry:
            return entry
    origin = getattr(spec, "origin", None)
    if type(origin) is str and origin:
        return os.path.dirname(origin)
    raise ChromiumProbeError("the playwright package has no directory")


def _registry_path() -> str:
    """The browser registry file this interpreter's playwright ships."""
    return os.path.join(_playwright_package_dir(), *_REGISTRY_RELPATH)


def _read_browser_registry(path: str) -> dict:
    """The registry, as an object. RAISES rather than guessing.

    A registry that cannot be read is the UNKNOWN case: guessing ABSENT here
    re-downloads 150 MB on every start, and guessing PRESENT turns the browser
    tools off with nothing said.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except Exception as exc:
        raise ChromiumProbeError(
            "the playwright browser registry could not be read ({})".format(
                type(exc).__name__))
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ChromiumProbeError(
            "the playwright browser registry is not readable JSON")
    if type(data) is not dict:
        raise ChromiumProbeError(
            "the playwright browser registry is not an object")
    return data


def _chromium_revision(registry) -> str:
    """The chromium build number the registry names.

    DEC-36: every value here came off disk, so its concrete type is pinned with
    `type(x) is T` in this frame before anything is done with it -- `.get`,
    `in` and `==` all dispatch.
    """
    if type(registry) is not dict:
        raise ChromiumProbeError("the browser registry is not an object")
    browsers = registry.get("browsers")
    if type(browsers) is not list:
        raise ChromiumProbeError("the browser registry lists no browsers")
    for entry in browsers:
        if type(entry) is not dict:
            continue
        name = entry.get("name")
        if type(name) is not str or name != "chromium":
            continue
        revision = entry.get("revision")
        if type(revision) is int:
            revision = str(revision)
        if type(revision) is not str or not revision.strip():
            raise ChromiumProbeError(
                "the browser registry names no chromium revision")
        text = revision.strip()
        if not set(text) <= _REVISION_CHARS:
            raise ChromiumProbeError(
                "the browser registry's chromium revision is not a build id")
        return text
    raise ChromiumProbeError("the browser registry names no chromium build")


def _default_browsers_root(platform: str) -> str:
    """Playwright's documented default cache directory for a platform.

    Replicated rather than imported: every helper that resolves this inside
    playwright lives under a module whose import starts the driver, which is
    the cost this finding removes.
    """
    plat = platform if type(platform) is str else ""
    if plat.startswith("win"):
        base = os.environ.get("LOCALAPPDATA")
        if type(base) is not str or not base.strip():
            base = os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, _BROWSERS_DIRNAME)
    if plat == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Caches",
                            _BROWSERS_DIRNAME)
    base = os.environ.get("XDG_CACHE_HOME")
    if type(base) is not str or not base.strip():
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, _BROWSERS_DIRNAME)


def _browsers_root(platform=None) -> str:
    """The directory playwright would install chromium into on this machine."""
    plat = platform if type(platform) is str else sys.platform
    override = os.environ.get(BROWSERS_PATH_ENV)
    text = override.strip() if type(override) is str else ""
    if text == "0":
        return os.path.join(_playwright_package_dir(), *_LOCAL_BROWSERS_RELPATH)
    if text:
        return text
    return _default_browsers_root(plat)


def _executable_candidates(platform=None) -> tuple:
    """The executable paths, relative to `chromium-<revision>/`, to look for.

    An unknown platform falls back to the linux layout rather than to nothing:
    an empty list would make the probe raise and read UNKNOWN forever, where a
    wrong guess reads ABSENT -- an answer the operator can act on.
    """
    plat = platform if type(platform) is str else sys.platform
    key = "win32" if plat.startswith("win") else plat
    layouts = _CHROMIUM_EXECUTABLES.get(key)
    if type(layouts) is not tuple:
        layouts = _CHROMIUM_EXECUTABLES["linux"]
    return tuple(os.path.join(*parts) for parts in layouts)


def _chromium_executable_path():
    """Where playwright's chromium is, or would be. A PURE FILESYSTEM ANSWER.

    THE DEFECT this replaces: this function used to open and close a whole
    `sync_playwright()` context just to read a path. That starts the Node
    driver, a greenlet and an event loop, and tearing that loop down inside a
    daemon start leaked `Task was destroyed but it is pending!`, a
    `Future exception was never retrieved` and a `playwright ... TargetClosedError`
    onto the console and into `daemon.log` -- on a start that exited 0 with the
    browser fully installed.

    Nothing here imports playwright. The registry says which build, the
    environment says where builds live, and the filesystem says whether the
    file is there. RAISES `ChromiumProbeError` when the question cannot be
    asked; `probe_chromium` catches that, in the one frame that can tell an
    unanswerable probe apart from an absent browser.
    """
    revision = _chromium_revision(_read_browser_registry(_registry_path()))
    build_dir = os.path.join(_browsers_root(), "chromium-{}".format(revision))
    candidates = [os.path.join(build_dir, rel)
                  for rel in _executable_candidates()]
    for path in candidates:
        try:
            if os.path.exists(path):
                return path
        except Exception:
            continue
    # Nothing on disk: the FIRST candidate is where an install would put it, so
    # an ABSENT verdict still names the place it looked.
    return candidates[0]


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
        # ONE line, and the traceback is NOT on it. `exc_info=True` here is the
        # second half of the witnessed symptom: it made 92 of a successful
        # `daemon start`'s 98 stderr lines a stack trace. The reason is what an
        # operator can act on; the stack is for a DEBUG run.
        logger.warning("[provision] the chromium probe could not run: %s",
                       reason)
        logger.debug("[provision] the chromium probe could not run",
                     exc_info=True)
        return ChromiumVerdict(CHROMIUM_UNKNOWN, reason)
    if present is True:
        return ChromiumVerdict(CHROMIUM_PRESENT, path=_witness_path())
    return ChromiumVerdict(CHROMIUM_ABSENT)


def _witness_path() -> str:
    """The executable a PRESENT verdict is about, for the log line. Never a
    decision: a path that cannot be re-read costs the line its detail and
    changes no state, so this swallows what `_chromium_executable_path` raises
    on purpose -- the frame above has already decided."""
    try:
        path = _chromium_executable_path()
    except Exception:
        logger.debug("[provision] could not name the present chromium",
                     exc_info=True)
        return ""
    return path if type(path) is str else ""


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


def _path_of(verdict) -> str:
    """The executable a verdict names, or "" -- for the LOG, never a decision."""
    if type(verdict) is not ChromiumVerdict:
        return ""
    path = verdict.path
    return path if type(path) is str else ""


#: What the PRESENT line says when the probe answered PRESENT but could not
#: hand back the path (a race with an uninstall, a permission error on the
#: re-read). The line still goes out: "the hook ran and found a browser" is the
#: half a silent branch could not tell an operator.
_UNREPORTED_PATH = "an unreported path"


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
        # P3 -- the ONE branch that used to return in silence. Every other
        # branch leaves a line, so a silent PRESENT was indistinguishable in
        # the log from a provision hook that never ran, and those have
        # different remedies. The console stays quiet: nothing to download is
        # nothing to interrupt the operator with.
        logger.info("[provision] chromium present at %s; nothing to download",
                    _ascii(_path_of(verdict) or _UNREPORTED_PATH))
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
