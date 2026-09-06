"""D10 -- the chromium auto-install is ANNOUNCED, with its opt-out, before it
starts.

THE DEFECT
    The first `systemu daemon start` on a fresh install spawns
    `python -m playwright install chromium` -- roughly 150 MB down the
    operator's connection -- and said so only through

        logger.info("[provision] chromium missing - spawning background install")

    The CLI configures logging at WARNING, so that line reaches nobody. An
    opt-out (`SYSTEMU_SKIP_BROWSER_AUTOINSTALL`) and a dashboard banner both
    existed; neither is on the console of the operator who just typed the
    command. A daemon start therefore began a large unrequested download in
    silence, and the variable that would have prevented it was discoverable
    only by reading the source.

THE PROPERTY
    Before the download is spawned, one plain line goes to the console naming
      * WHAT is being downloaded -- Playwright's Chromium,
      * roughly HOW BIG it is,
      * WHY -- the web tools that need a browser,
      * that it runs in the BACKGROUND, and
      * the exact env var that turns it off.
    The opt-out suppresses the spawn and says so. And because the daemon
    child's stdout is redirected into `daemon.log`, the PARENT announces it
    too, before spawning the child -- that is the copy the operator sees.

NO REAL DOWNLOAD AND NO REAL DAEMON. `subprocess.Popen` is monkeypatched at
the module for the chromium spawn and at the stdlib for the daemon spawn; the
product's own functions are never replaced by stand-ins that decide the
outcome under test.
"""
from __future__ import annotations

import socket
import subprocess as _stdlib_subprocess
import sys
from types import SimpleNamespace

import pytest

import systemu.runtime.web.provision as provision


ENV = "SYSTEMU_SKIP_BROWSER_AUTOINSTALL"


@pytest.fixture()
def clean_provision(monkeypatch):
    """A module that has not yet bootstrapped, with the opt-out unset.

    `_bootstrapped` is process-global by design (the install is once per
    daemon); resetting it here keeps one test from deciding the next.
    """
    monkeypatch.setattr(provision, "_bootstrapped", False)
    monkeypatch.delenv(ENV, raising=False)
    return provision


class _Recorder:
    """Collects the ORDER of the two things that must not swap places."""

    def __init__(self):
        self.events = []
        self.lines = []

    def console(self, line):
        self.events.append("console")
        self.lines.append(line)

    def popen(self, *args, **kwargs):
        self.events.append("spawn")
        self.argv = args[0] if args else None
        return SimpleNamespace(pid=4242)


# -- the order, and the line ------------------------------------------------

def test_the_console_line_comes_before_the_spawn(clean_provision, monkeypatch):
    """Announcing a download after starting it is the same silence with extra
    steps: by then the bytes are already moving."""
    rec = _Recorder()
    monkeypatch.setattr(provision, "chromium_present", lambda: False)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)

    assert rec.events == ["console", "spawn"], rec.events
    assert rec.argv[1:] == ["-m", "playwright", "install", "chromium"], rec.argv


def test_the_line_names_what_how_big_why_where_and_the_opt_out(clean_provision,
                                                               monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(provision, "chromium_present", lambda: False)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)

    assert len(rec.lines) == 1, rec.lines
    line = rec.lines[0]
    low = line.lower()
    assert "chromium" in low, line               # WHAT
    assert "playwright" in low, line             # whose
    assert "150 mb" in low, line                 # roughly HOW BIG
    assert "background" in low, line             # WHERE it runs
    assert ENV in line, line                     # the exact opt-out
    for capability in ("screenshot", "javascript", "interaction"):
        assert capability in low, (capability, line)   # WHY


def test_the_opt_out_suppresses_the_spawn_and_says_it_was_skipped(
        clean_provision, monkeypatch):
    """The opt-out must be what stops the spawn -- not the accident of this
    machine already having chromium.

    `chromium_present` is made to RAISE rather than return False: a test that
    let the presence probe answer would go green on a developer box with
    chromium installed even if the opt-out had stopped working entirely, which
    is exactly how this fence was found unwitnessed.
    """
    rec = _Recorder()
    monkeypatch.setenv(ENV, "true")
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    def must_not_run():
        raise AssertionError("probed for chromium after the operator opted out")

    monkeypatch.setattr(provision, "chromium_present", must_not_run)

    provision.ensure_chromium_async(console=rec.console)

    assert "spawn" not in rec.events, rec.events
    assert len(rec.lines) == 1, rec.lines
    assert ENV in rec.lines[0], rec.lines[0]
    assert "not downloading" in rec.lines[0].lower(), rec.lines[0]
    # ...and the way to do it later, by hand.
    assert "playwright install chromium" in rec.lines[0], rec.lines[0]


def test_a_present_chromium_neither_announces_nor_spawns(clean_provision,
                                                         monkeypatch):
    """Nothing to download is nothing to say. A line on every start would be
    noise, and noise is how a real notice gets scrolled past."""
    rec = _Recorder()
    monkeypatch.setattr(provision, "chromium_present", lambda: True)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    provision.ensure_chromium_async(console=rec.console)

    assert rec.events == [], rec.events


def test_a_failed_spawn_retracts_the_announcement(clean_provision, monkeypatch):
    """An announcement that turns out to be false is worse than none: the
    operator would sit waiting for browser tools that were never coming."""
    rec = _Recorder()

    def exploding_popen(*args, **kwargs):
        rec.events.append("spawn")
        raise OSError("no such file")

    monkeypatch.setattr(provision, "chromium_present", lambda: False)
    monkeypatch.setattr(provision.subprocess, "Popen", exploding_popen)

    provision.ensure_chromium_async(console=rec.console)

    assert rec.events == ["console", "spawn", "console"], rec.events
    assert len(rec.lines) == 2, rec.lines
    assert "could not start" in rec.lines[1].lower(), rec.lines[1]
    assert "playwright install chromium" in rec.lines[1], rec.lines[1]


def test_an_already_bootstrapped_module_says_nothing(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(provision, "_bootstrapped", True)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    provision.ensure_chromium_async(console=rec.console)

    assert rec.events == [], rec.events


# -- the parent's copy, which is the one the operator sees ------------------

def test_announce_names_the_download_when_chromium_is_missing(clean_provision,
                                                              monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(provision, "chromium_present", lambda: False)

    line = provision.announce_browser_autoinstall(console=rec.console)

    assert rec.lines == [line], (rec.lines, line)
    assert "chromium" in line.lower(), line
    assert ENV in line, line


def test_announce_says_nothing_when_chromium_is_present(clean_provision,
                                                        monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(provision, "chromium_present", lambda: True)

    line = provision.announce_browser_autoinstall(console=rec.console)

    assert line is None, line
    assert rec.lines == [], rec.lines


def test_announce_reports_the_opt_out_without_probing_for_chromium(
        clean_provision, monkeypatch):
    """The opt-out is the operator's answer; asking playwright about it after
    they said no is work nobody asked for."""
    rec = _Recorder()
    monkeypatch.setenv(ENV, "true")

    def must_not_run():
        raise AssertionError("probed for chromium after the operator opted out")

    monkeypatch.setattr(provision, "chromium_present", must_not_run)

    line = provision.announce_browser_autoinstall(console=rec.console)

    assert ENV in line, line
    assert rec.lines == [line], rec.lines


def test_the_opt_out_reads_the_operators_value_not_a_prefix(clean_provision,
                                                            monkeypatch):
    monkeypatch.setenv(ENV, "  TRUE  ")
    assert provision.opted_out() is True
    monkeypatch.setenv(ENV, "false")
    assert provision.opted_out() is False
    monkeypatch.setenv(ENV, "")
    assert provision.opted_out() is False
    monkeypatch.delenv(ENV, raising=False)
    assert provision.opted_out() is False


# -- the console the notice actually lands on -------------------------------

def test_the_default_writer_puts_the_line_on_stderr(capsys):
    provision._console_write("systemu: hello")
    captured = capsys.readouterr()
    assert captured.err == "systemu: hello\n", captured.err
    assert captured.out == "", captured.out


def test_a_console_less_process_does_not_crash_the_start(monkeypatch):
    """pythonw has no stderr. Losing the line there must not lose the daemon."""
    monkeypatch.setattr(sys, "stderr", None)
    provision._console_write("systemu: hello")


def test_the_notices_are_ascii():
    """DEC-32c: a notice a cp1252 console cannot encode is a notice the
    operator never reads -- and, written at daemon start, it would raise
    UnicodeEncodeError instead."""
    provision.autoinstall_notice().encode("ascii")
    provision.autoinstall_skipped_notice().encode("ascii")
    provision.autoinstall_failed_notice("café").encode("ascii")


# -- the reachability pin: the parent really calls it, before the spawn -----

def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_start_daemon_announces_the_download_before_spawning_the_child(
        tmp_path, monkeypatch, clean_provision):
    """THE PIN (not just a unit test): remove the call from `start_daemon` and
    this goes red.

    The daemon child's stdout is redirected into `daemon.log`, so the child's
    own notice never reaches a console. This is the copy the operator sees, and
    it must precede the spawn it is about.
    """
    import systemu.scheduler.daemon as daemon_mod

    vault = tmp_path / "home" / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault))
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PORT", raising=False)

    rec = _Recorder()
    monkeypatch.setattr(provision, "chromium_present", lambda: False)
    monkeypatch.setattr(provision, "_console_write", rec.console)
    # The DAEMON child's spawn -- `start_daemon` does `import subprocess`
    # inside the function, so the stdlib attribute is the seam.
    monkeypatch.setattr(_stdlib_subprocess, "Popen", rec.popen)

    verdict = daemon_mod.start_daemon(
        vault_dir=str(vault), config=SimpleNamespace(), vault=SimpleNamespace(),
        port=_closed_port(), foreground=False, wait_timeout_s=0.0,
    )

    assert verdict is not None
    assert rec.events == ["console", "spawn"], rec.events
    assert "chromium" in rec.lines[0].lower(), rec.lines[0]
    assert ENV in rec.lines[0], rec.lines[0]


def test_start_daemon_says_nothing_when_chromium_is_already_there(
        tmp_path, monkeypatch, clean_provision):
    """The pin's own control: with chromium installed the start is as quiet as
    it always was, so the test above cannot pass on an unconditional print."""
    import systemu.scheduler.daemon as daemon_mod

    vault = tmp_path / "home" / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault))
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PORT", raising=False)

    rec = _Recorder()
    monkeypatch.setattr(provision, "chromium_present", lambda: True)
    monkeypatch.setattr(provision, "_console_write", rec.console)
    monkeypatch.setattr(_stdlib_subprocess, "Popen", rec.popen)

    daemon_mod.start_daemon(
        vault_dir=str(vault), config=SimpleNamespace(), vault=SimpleNamespace(),
        port=_closed_port(), foreground=False, wait_timeout_s=0.0,
    )

    assert rec.events == ["spawn"], rec.events
