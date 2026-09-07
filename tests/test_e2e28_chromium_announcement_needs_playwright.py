"""N2 -- the Chromium announcement is only made when playwright can actually
run it, and an install that fails is reported.

THE DEFECT (e2e regression of v0.10.28, documented install)
    ``pip install "systemu[dashboard]"`` -- the command the docs give for the
    dashboard -- does NOT pull ``playwright``: that lives under the ``browser``
    and ``all`` extras. On exactly that install, ``systemu daemon start``:

      * printed "downloading it in the background now (roughly 150 MB) so the
        web tools can work",
      * logged "[provision] chromium missing -- spawning background install",
      * spawned ``python -m playwright install chromium``, which died instantly
        with ``No module named playwright``.

    The child's stdout and stderr went to ``DEVNULL`` and ``Popen`` does not
    raise for a child that starts and then exits non-zero, so
    ``autoinstall_failed_notice()`` never fired. The browsers directory stayed
    empty, and the operator was told a 150 MB download was under way that was
    never going to happen -- with no output kept anywhere to say otherwise. The
    opt-out notice's remedy (``python -m playwright install chromium``) failed
    the same way for the same reason.

THE PROPERTIES
    1. IMPORTABILITY IS PROBED FIRST. With playwright absent there is no
       download announcement and no spawn -- one plain line names the two-step
       remedy instead (the extra, then the browser download).
    2. AN ANNOUNCED INSTALL IS WATCHED. When playwright is present and chromium
       is missing, the child's output is captured to ``browser_install.log``
       beside the daemon's own log, and a waiter reports a non-zero exit with
       the code and that path. A zero exit says nothing.
    3. A provisioning side effect NEVER MINTS a vault directory: with no daemon
       log to sit beside, the install still runs and simply keeps no log.

NO REAL DOWNLOAD AND NO REAL DAEMON: ``subprocess.Popen`` is monkeypatched at
the module, and the vault lives under ``tmp_path`` via ``SYSTEMU_VAULT_DIR``.
"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

import systemu.runtime.web.provision as provision


ENV = "SYSTEMU_SKIP_BROWSER_AUTOINSTALL"
VAULT_ENV = "SYSTEMU_VAULT_DIR"


@pytest.fixture()
def clean_provision(monkeypatch):
    """A module that has not yet bootstrapped, with the opt-out unset."""
    monkeypatch.setattr(provision, "_bootstrapped", False)
    monkeypatch.setattr(provision, "_install_waiter", None, raising=False)
    monkeypatch.delenv(ENV, raising=False)
    return provision


class _Recorder:
    """The ORDER of console lines and spawns, plus what the spawn was handed."""

    def __init__(self, *, exit_code=0):
        self.events = []
        self.lines = []
        self.argv = None
        self.stdout = None
        self.stderr = None
        self._exit_code = exit_code

    def console(self, line):
        self.events.append("console")
        self.lines.append(line)

    def popen(self, *args, **kwargs):
        self.events.append("spawn")
        self.argv = args[0] if args else None
        self.stdout = kwargs.get("stdout")
        self.stderr = kwargs.get("stderr")
        code = self._exit_code
        return SimpleNamespace(pid=4242, wait=lambda *a, **k: code)


def _vault_with_a_daemon_log(tmp_path, monkeypatch):
    """A vault directory that a daemon has already logged into.

    The real child's stdout IS ``daemon.log``, opened by the parent before the
    spawn, so this is the state the provision probe actually runs in.
    """
    vault = tmp_path / "home" / "vault"
    vault.mkdir(parents=True)
    (vault / "daemon.log").write_text("", encoding="ascii")
    monkeypatch.setenv(VAULT_ENV, str(vault))
    return vault


def _join_the_waiter(timeout=15.0):
    waiter = getattr(provision, "_install_waiter", None)
    assert waiter is not None, "no waiter was started for the announced install"
    waiter.join(timeout)
    assert not waiter.is_alive(), "the install waiter never finished"


# -- property 1: no playwright, no announcement, no spawn -------------------

def test_a_missing_playwright_neither_announces_a_download_nor_spawns(
        clean_provision, monkeypatch):
    """THE repro. `pip install "systemu[dashboard]"` leaves playwright absent;
    announcing a 150 MB download there is a promise the box cannot keep."""
    rec = _Recorder()
    monkeypatch.setattr(provision, "playwright_importable", lambda: False)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    # The importability probe must come FIRST. `chromium_present` is made to
    # RAISE rather than return False, exactly as the opt-out test next door
    # does: letting the presence probe answer would make this test's verdict
    # depend on whether the box running it happens to have chromium, and an
    # implementation that asked playwright about its browser before asking
    # whether playwright is there at all would still go green on a bare one.
    def must_not_run():
        raise AssertionError("probed for chromium without playwright installed")

    monkeypatch.setattr(provision, "chromium_present", must_not_run)

    provision.ensure_chromium_async(console=rec.console)

    assert "spawn" not in rec.events, rec.events
    assert len(rec.lines) == 1, rec.lines
    line = rec.lines[0]
    assert provision.autoinstall_notice() not in rec.lines, rec.lines
    assert "150 mb" not in line.lower(), line
    # ...and the ONE thing the operator can do about it, both steps of it.
    assert 'pip install "systemu[browser]"' in line, line
    assert "python -m playwright install chromium" in line, line


def test_the_parents_announcement_is_silent_about_a_download_it_cannot_start(
        clean_provision, monkeypatch):
    """`daemon start` announces from the PARENT -- that is the copy the
    operator reads, and it was the one making the false promise."""
    rec = _Recorder()
    monkeypatch.setattr(provision, "playwright_importable", lambda: False)

    def must_not_run():
        raise AssertionError("probed for chromium without playwright installed")

    monkeypatch.setattr(provision, "chromium_present", must_not_run)

    line = provision.announce_browser_autoinstall(console=rec.console)

    assert rec.lines == [line], (rec.lines, line)
    assert "150 mb" not in line.lower(), line
    assert 'pip install "systemu[browser]"' in line, line
    assert "python -m playwright install chromium" in line, line


def test_the_opt_out_remedy_is_the_two_step_one_when_playwright_is_absent(
        clean_provision, monkeypatch):
    """The opt-out's remedy failed in exactly the same way it was announced:
    `python -m playwright install chromium` cannot run without playwright."""
    monkeypatch.setattr(provision, "playwright_importable", lambda: False)

    line = provision.autoinstall_skipped_notice()

    assert 'pip install "systemu[browser]"' in line, line
    assert "python -m playwright install chromium" in line, line


def test_the_opt_out_remedy_stays_one_step_when_playwright_is_installed(
        clean_provision, monkeypatch):
    """The control: an operator who already has the wheel must not be told to
    reinstall it. A remedy that names work already done is noise."""
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)

    line = provision.autoinstall_skipped_notice()

    assert "python -m playwright install chromium" in line, line
    assert "pip install" not in line, line


def test_every_notice_stays_ascii(monkeypatch):
    """DEC-32c: a notice a cp1252 console cannot encode raises instead of
    printing, at daemon start."""
    for absent in (True, False):
        monkeypatch.setattr(provision, "playwright_importable", lambda: absent)
        provision.playwright_missing_notice().encode("ascii")
        provision.autoinstall_skipped_notice().encode("ascii")
        provision.autoinstall_failed_notice("exit code 1").encode("ascii")


# -- property 2: an announced install is watched ----------------------------

@pytest.mark.parametrize("code", [1, 9])
def test_a_failed_install_is_reported_with_its_code_and_its_log(
        clean_provision, monkeypatch, tmp_path, code):
    """Popen does not raise for a child that starts and then dies. Without a
    waiter the operator is left holding an announcement that never came true.

    1 is what `No module named playwright` actually exits with -- the observed
    defect. The second code is here because the exit code must be READ off the
    child rather than spelled into the message: a notice that always says
    "exit code 1" would satisfy the first case alone.
    """
    vault = _vault_with_a_daemon_log(tmp_path, monkeypatch)
    rec = _Recorder(exit_code=code)
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)
    monkeypatch.setattr(provision, "chromium_present", lambda: False)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)
    _join_the_waiter()

    assert rec.events == ["console", "spawn", "console"], rec.events
    failure = rec.lines[1]
    assert "could not start" in failure.lower(), failure
    # NOT a bare `"1" in failure`: the log path below is a pytest tmp dir whose
    # own name carries digits, so the bare form is true of every message this
    # function could possibly return, including one that never names the code.
    assert "exit code {}".format(code) in failure.lower(), failure
    log_path = str(vault / "browser_install.log")
    assert log_path in failure, (failure, log_path)


def test_the_install_log_sits_beside_the_daemons_own_log(
        clean_provision, monkeypatch, tmp_path):
    """The child's output went to DEVNULL, which is why the instant death left
    nothing to read. It is captured now, in the directory the operator already
    knows to look in."""
    vault = _vault_with_a_daemon_log(tmp_path, monkeypatch)
    rec = _Recorder(exit_code=0)
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)
    monkeypatch.setattr(provision, "chromium_present", lambda: False)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)
    _join_the_waiter()

    assert rec.stdout is not None and rec.stdout is not subprocess.DEVNULL
    assert str(getattr(rec.stdout, "name", "")) == str(
        vault / "browser_install.log"), rec.stdout
    assert rec.stderr == subprocess.STDOUT, rec.stderr
    assert (vault / "browser_install.log").exists()
    assert (vault / "daemon.log").exists()


def test_a_successful_install_says_nothing_more(
        clean_provision, monkeypatch, tmp_path):
    """The control for the waiter: a retraction on every install would be a
    lie in the other direction, and the test above would pass on one."""
    _vault_with_a_daemon_log(tmp_path, monkeypatch)
    rec = _Recorder(exit_code=0)
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)
    monkeypatch.setattr(provision, "chromium_present", lambda: False)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)
    _join_the_waiter()

    assert rec.events == ["console", "spawn"], rec.events
    assert len(rec.lines) == 1, rec.lines


# -- property 3: provisioning never mints a vault ---------------------------

def test_with_no_daemon_log_to_sit_beside_the_install_still_runs_and_writes_nothing(
        clean_provision, monkeypatch, tmp_path):
    """A background convenience must not be what CREATES the operating vault,
    or a probe run from the wrong directory quietly mints one."""
    vault = tmp_path / "nowhere" / "vault"
    monkeypatch.setenv(VAULT_ENV, str(vault))
    rec = _Recorder(exit_code=0)
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)
    monkeypatch.setattr(provision, "chromium_present", lambda: False)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)

    assert rec.events == ["console", "spawn"], rec.events
    assert rec.stdout == subprocess.DEVNULL, rec.stdout
    assert not vault.exists(), sorted(p.name for p in vault.iterdir())


# -- the importability probe itself -----------------------------------------

def test_the_probe_answers_for_this_interpreter():
    """It is `find_spec`, not a try/import: importing playwright to find out
    whether it is importable costs the whole package at daemon start."""
    import importlib.util

    value = provision.playwright_importable()
    assert type(value) is bool, type(value)
    assert value is (importlib.util.find_spec("playwright") is not None)


def test_the_probe_fails_closed_when_find_spec_explodes(monkeypatch):
    """A probe that cannot answer must not announce a download."""
    import importlib.util

    def boom(name):
        raise ImportError("broken meta path")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert provision.playwright_importable() is False
