"""P2 -- a healthy `daemon start` prints asyncio ERRORs and a Playwright traceback.

THE WITNESSED DEFECT (dogfood of the `[browser]` install, Chromium present)
--------------------------------------------------------------------------
`systemu daemon start` exited 0, said `OK Daemon ready.`, and then printed, on
stderr and again into `daemon.log`::

    [asyncio] ERROR: Task was destroyed but it is pending!
    Future exception was never retrieved
    playwright._impl._errors.TargetClosedError

On the UNKNOWN branch it was worse: `logger.warning(..., exc_info=True)` made
92 of the start's 98 stderr lines a traceback, on a start that had succeeded.

THE CAUSE was the probe itself. `provision._chromium_executable_path` opened
and closed a whole `sync_playwright()` context -- which starts the Node driver,
a greenlet and an event loop -- purely to READ A PATH. The teardown of that
loop is what leaked the pending task and the `TargetClosedError`; the browser
was fine, the daemon was fine, and the operator was handed a stack trace.

THE PROPERTY PINNED HERE
    The Chromium probe is a PURE FILESYSTEM CHECK. It reads the installed
    playwright's own browser registry (`driver/package/browsers.json`, which
    names the chromium build `revision`), computes the install directory under
    `PLAYWRIGHT_BROWSERS_PATH` or the platform's documented default cache, and
    asks the filesystem whether the executable is there. It never starts the
    driver, so there is no loop to tear down and nothing to leak.

      * PRESENT -- the executable exists.
      * ABSENT  -- the registry was readable and the executable is not there.
      * UNKNOWN(reason) -- the registry is unreadable, playwright cannot be
        located, or anything else went wrong. ONE warning line naming the
        reason; the traceback goes to DEBUG, where a healthy start does not
        print it.

NOTHING HERE LAUNCHES A DRIVER OR DOWNLOADS A BROWSER. The registry and the
browsers directory are both tmp dirs, and the reachability pins below are what
keep that true of production too.
"""
from __future__ import annotations

import ast
import io
import json
import logging
import os
import sys
from pathlib import Path

import pytest

import systemu.runtime.web.provision as provision


#: The build the fake registry names. Not a real revision -- the point is that
#: the probe uses whatever the registry says.
FAKE_REVISION = "9911"

#: The names that mean "the driver was started". Neither may appear anywhere on
#: the probe's path.
DRIVER_NAMES = ("sync_playwright", "async_playwright")


# --------------------------------------------------------------------------- #
# the two seams: the playwright package dir, and the browsers dir
# --------------------------------------------------------------------------- #

def _fake_package(tmp_path, registry_text):
    """A tmp dir shaped like an installed playwright package, with a registry.

    `registry_text=None` writes no registry at all, which is the "unreadable"
    case an operator gets from a half-finished install.
    """
    pkg = tmp_path / "site-packages" / "playwright"
    (pkg / "driver" / "package").mkdir(parents=True, exist_ok=True)
    registry = pkg / "driver" / "package" / "browsers.json"
    if registry_text is None:
        if registry.exists():
            registry.unlink()
    else:
        registry.write_text(registry_text, encoding="utf-8")
    return pkg


def _registry(revision=FAKE_REVISION):
    return json.dumps({
        "comment": "fake",
        "browsers": [
            {"name": "chromium", "revision": revision,
             "browserVersion": "147.0.0.0", "installByDefault": True},
            {"name": "firefox", "revision": "1500", "installByDefault": True},
        ],
    })


@pytest.fixture()
def probe_env(tmp_path, monkeypatch):
    """A probe whose registry and browsers directory are both under tmp_path.

    The real playwright install on the developer's machine is unreachable from
    here: the package dir is replaced at the seam and the browsers directory is
    forced by the env var playwright itself honours.
    """
    browsers = tmp_path / "ms-playwright"
    browsers.mkdir()
    monkeypatch.setenv(provision.BROWSERS_PATH_ENV, str(browsers))

    def _install(registry_text):
        pkg = _fake_package(tmp_path, registry_text)
        monkeypatch.setattr(provision, "_playwright_package_dir",
                            lambda: str(pkg))
        return pkg

    _install(_registry())
    return type("ProbeEnv", (), {"browsers": browsers, "install": staticmethod(_install)})


def _place_executable(browsers, revision=FAKE_REVISION):
    """Put a chromium where the probe is entitled to look for it, in THIS
    platform's layout -- the first candidate the implementation names."""
    relative = provision._executable_candidates()[0]
    exe = Path(browsers) / "chromium-{}".format(revision) / relative
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("", encoding="ascii")
    return exe


# --------------------------------------------------------------------------- #
# 1. the three states, off the filesystem
# --------------------------------------------------------------------------- #

def test_an_executable_on_disk_is_PRESENT(probe_env):
    exe = _place_executable(probe_env.browsers)

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_PRESENT, verdict
    assert verdict.reason == "", verdict.reason
    assert verdict.path == str(exe), (verdict.path, str(exe))


def test_a_readable_registry_with_no_executable_on_disk_is_ABSENT(probe_env):
    """The one state a download may follow from: the registry ANSWERED, and the
    binary it names is not there."""
    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_ABSENT, verdict


def test_a_missing_registry_is_UNKNOWN_and_carries_its_reason(probe_env):
    """A half-finished or trimmed playwright install. Not an absent browser --
    the question was not answered, and an unanswered question must not spend the
    operator's connection."""
    probe_env.install(None)

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_UNKNOWN, verdict
    assert "registry" in verdict.reason.lower(), verdict.reason
    verdict.reason.encode("ascii")


@pytest.mark.parametrize("registry_text, label", [
    ("not json at all", "not JSON"),
    ("[]", "a list, not an object"),
    ("{}", "no browsers key"),
    (json.dumps({"browsers": {"chromium": "1217"}}), "browsers is an object"),
    (json.dumps({"browsers": [{"name": "firefox", "revision": "1500"}]}),
     "no chromium entry"),
    (json.dumps({"browsers": [{"name": "chromium"}]}), "no revision"),
    (json.dumps({"browsers": [{"name": "chromium", "revision": None}]}),
     "a null revision"),
    (json.dumps({"browsers": [{"name": "chromium", "revision": "../../etc"}]}),
     "a revision that is a path traversal"),
    (json.dumps({"browsers": ["chromium-1217"]}), "browsers of strings"),
])
def test_a_registry_of_an_unexpected_shape_is_UNKNOWN(probe_env, registry_text,
                                                      label):
    """Every shape that is not the one documented reads as "could not tell".

    Reading a strange registry OPTIMISTICALLY is the same defect one floor up:
    a guess that lands on ABSENT re-downloads 150 MB on every start, and a guess
    that lands on PRESENT turns the browser tools off with no explanation.
    """
    probe_env.install(registry_text)

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_UNKNOWN, (label, verdict)
    assert verdict.reason, label
    verdict.reason.encode("ascii")


def test_a_playwright_that_cannot_be_located_is_UNKNOWN(monkeypatch):
    monkeypatch.setattr(provision.importlib.util, "find_spec",
                        lambda name: None)

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_UNKNOWN, verdict
    assert "playwright" in verdict.reason.lower(), verdict.reason


def test_the_three_states_stay_distinct():
    """A mutation that collapses UNKNOWN into ABSENT has to make two of these
    equal, which is louder than a quiet fallthrough."""
    assert len({provision.CHROMIUM_PRESENT, provision.CHROMIUM_ABSENT,
                provision.CHROMIUM_UNKNOWN}) == 3


# --------------------------------------------------------------------------- #
# 2. the probe never starts the driver -- the reachability pins
# --------------------------------------------------------------------------- #

def test_provision_names_the_playwright_driver_nowhere(probe_env):
    """THE REACHABILITY PIN, as an AST walk rather than a substring scan: a
    comment or a docstring may DISCUSS the sync API (this module's history is
    the reason it must not use it), but no name, attribute, import or alias may
    be it.

    Bring `from playwright.sync_api import sync_playwright` back into the probe
    and this goes red -- which is the mutation the whole finding turns on.
    """
    source = io.open(provision.__file__, encoding="utf-8").read()
    tree = ast.parse(source)

    named = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in DRIVER_NAMES:
            named.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in DRIVER_NAMES:
            named.append((node.lineno, node.attr))
        elif isinstance(node, ast.alias):
            for part in (node.name or "", node.asname or ""):
                if part.rsplit(".", 1)[-1] in DRIVER_NAMES:
                    named.append((getattr(node, "lineno", 0), part))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").endswith("sync_api"):
                named.append((node.lineno, node.module))

    assert not named, (
        "the chromium probe starts the playwright driver again; that driver's "
        "loop teardown is what printed a TargetClosedError on a healthy "
        "`daemon start`: " + repr(named))


def test_probing_imports_no_playwright_module(probe_env, monkeypatch):
    """The runtime half of the pin above, and a different lens on it: after a
    full probe, no playwright submodule has been executed into `sys.modules`.

    `find_spec` is allowed -- locating a package does not run it. Importing
    `playwright.sync_api` is what pulls in greenlet, the driver and a loop.
    """
    for name in [n for n in list(sys.modules) if n.startswith("playwright")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    _place_executable(probe_env.browsers)

    provision.probe_chromium()

    loaded = sorted(n for n in sys.modules if n.startswith("playwright"))
    assert loaded == [], (
        "the probe executed playwright code: " + repr(loaded))


# --------------------------------------------------------------------------- #
# 3. the UNKNOWN line: one warning, and the traceback only at DEBUG
# --------------------------------------------------------------------------- #

def test_an_unknown_probe_logs_one_warning_and_no_traceback(probe_env, caplog):
    """THE OTHER HALF OF THE WITNESSED SYMPTOM: `logger.warning(...,
    exc_info=True)` turned 92 of a successful start's 98 stderr lines into a
    stack trace. The reason belongs on the warning; the traceback belongs at
    DEBUG, where a healthy start never prints it."""
    probe_env.install(None)
    caplog.set_level(logging.DEBUG, logger=provision.logger.name)

    provision.probe_chromium()

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING
                and r.name == provision.logger.name]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert warnings[0].exc_info is None, (
        "the probe's warning still carries a traceback, which is what made a "
        "healthy `daemon start` print 92 lines of stack")
    assert "could not run" in warnings[0].getMessage().lower()
    debugs = [r for r in caplog.records
              if r.levelno == logging.DEBUG and r.exc_info is not None]
    assert debugs, "the traceback was not kept anywhere; DEBUG is where it goes"


# --------------------------------------------------------------------------- #
# 4. where the probe looks: the documented locations, per platform
# --------------------------------------------------------------------------- #

def test_the_browsers_path_env_wins(probe_env, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv(provision.BROWSERS_PATH_ENV, str(elsewhere))

    assert provision._browsers_root() == str(elsewhere)


def test_the_documented_zero_puts_the_browsers_inside_the_package(probe_env,
                                                                  monkeypatch):
    """`PLAYWRIGHT_BROWSERS_PATH=0` is playwright's documented "keep them in the
    wheel" install. Reading it as a DIRECTORY NAMED "0" would look in a relative
    path that does not exist and call a present browser absent."""
    monkeypatch.setenv(provision.BROWSERS_PATH_ENV, "0")

    root = provision._browsers_root()

    assert root.endswith(os.path.join("driver", "package", ".local-browsers")), root
    assert "playwright" in root


@pytest.mark.parametrize("platform, env, expected_tail", [
    ("win32", {"LOCALAPPDATA": os.path.join("C:", "Users", "x", "AppData", "Local")},
     os.path.join("Local", "ms-playwright")),
    ("darwin", {}, os.path.join("Library", "Caches", "ms-playwright")),
    ("linux", {"XDG_CACHE_HOME": os.path.join("/tmp", "cache")},
     os.path.join("cache", "ms-playwright")),
])
def test_the_platform_default_cache_dirs_are_the_documented_ones(
        platform, env, expected_tail, monkeypatch):
    """Replicated from playwright's own documented defaults. A wrong default is
    an ABSENT verdict on a machine where chromium is installed -- which is the
    150 MB re-download this finding's sibling closed."""
    monkeypatch.delenv(provision.BROWSERS_PATH_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    root = provision._default_browsers_root(platform)

    assert root.endswith(expected_tail), (platform, root)


@pytest.mark.parametrize("platform, must_contain", [
    ("win32", "chrome.exe"),
    ("darwin", "Chromium"),
    ("linux", "chrome"),
])
def test_each_platform_names_its_own_executable_layout(platform, must_contain):
    candidates = provision._executable_candidates(platform)

    assert candidates, platform
    assert any(must_contain in c for c in candidates), (platform, candidates)
    assert all(type(c) is str and c for c in candidates), candidates


def test_an_unknown_platform_still_names_a_candidate():
    """An empty candidate list would make the probe raise on an exotic platform
    and read as UNKNOWN forever. Falling back to the linux layout is a guess
    that can be WRONG, which is ABSENT -- an honest answer -- rather than a
    crash."""
    assert provision._executable_candidates("aix7") == \
        provision._executable_candidates("linux")


def test_the_probe_reads_the_revision_the_registry_names(probe_env):
    """Not a hard-coded build number: the registry is the only thing that knows
    which chromium THIS playwright wants."""
    probe_env.install(_registry(revision="4242"))
    _place_executable(probe_env.browsers, revision="4242")

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_PRESENT, verdict
    assert "chromium-4242" in verdict.path, verdict.path


def test_a_browser_from_a_different_revision_is_ABSENT(probe_env):
    """The control for the test above: a chromium on disk under the WRONG
    revision is not the one this playwright will launch."""
    probe_env.install(_registry(revision="4242"))
    _place_executable(probe_env.browsers, revision="1111")

    assert provision.probe_chromium().state == provision.CHROMIUM_ABSENT
