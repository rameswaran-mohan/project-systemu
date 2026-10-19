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

WHICH INSTALL THIS RUNS ON -- D8 (GATE-6). Every test here used to pin the
machine with `chromium_present` alone. Two later changes moved the seam out
from under that pin: 0.10.30 put an IMPORTABILITY gate in front of the
presence probe (`pip install "systemu[dashboard]"` ships no playwright, and on
that install the announcement path returns at the gate), and 0.10.31 turned
the presence probe into a filesystem check behind a tri-state
`probe_chromium` -> `ChromiumVerdict`. On an install without the `browser`
extra the pinned seam was therefore never reached: eight of these tests
stopped testing the announcement property and started failing on the
"Playwright is not installed" line -- and with the extra installed all 64
chromium tests passed, so nothing said so.

So the install is pinned at BOTH seams the decision frames consult:
`playwright_importable` and the presence answer under `probe_chromium`. No
assertion is weakened -- a pin says which machine, never what the product
should print. `test_the_pinned_seams_*` are the positive control: they patch
each seam to RAISE and require the product path to carry the exception out,
so a product that stops consulting one of them fails loudly here instead of
leaving these tests quietly pinning nothing.
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


class _SeamNotConsulted(Exception):
    """Raised BY a pinned seam, to prove the product really asks it."""


#: The genuine tri-state probe, held before any test can pin over it. The pin
#: below derives its verdict by RUNNING this, so a second `_pin_install` in one
#: test cannot end up asking the first one's stand-in what the machine is.
_REAL_PROBE_CHROMIUM = provision.probe_chromium


def _pin_install(monkeypatch, *, playwright=True,
                 chromium=provision.CHROMIUM_ABSENT):
    """Pin the install these tests are about: playwright importable or not,
    and chromium PRESENT / ABSENT / UNKNOWN.

    The presence answer is pinned at `chromium_present`, and the verdict the
    REAL `probe_chromium` derives from it is then pinned at `probe_chromium`,
    so both seams are fixed and it is still the product's own tri-state
    wrapping that decided which verdict they are fixed to. Hand-building a
    `ChromiumVerdict` here would be a stand-in deciding the outcome under test.
    """
    monkeypatch.setattr(provision, "playwright_importable", lambda: playwright)

    if chromium is provision.CHROMIUM_UNKNOWN:
        def present():
            raise provision.ChromiumProbeError("pinned: the probe cannot run")
    else:
        answer = chromium is provision.CHROMIUM_PRESENT

        def present():
            return answer

    monkeypatch.setattr(provision, "chromium_present", present)
    verdict = _REAL_PROBE_CHROMIUM()
    assert verdict.state is chromium, (verdict, chromium)
    monkeypatch.setattr(provision, "probe_chromium", lambda: verdict)


# -- the order, and the line ------------------------------------------------

def test_the_console_line_comes_before_the_spawn(clean_provision, monkeypatch):
    """Announcing a download after starting it is the same silence with extra
    steps: by then the bytes are already moving."""
    rec = _Recorder()
    _pin_install(monkeypatch, chromium=provision.CHROMIUM_ABSENT)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)

    assert rec.events == ["console", "spawn"], rec.events
    assert rec.argv[1:] == ["-m", "playwright", "install", "chromium"], rec.argv


def test_the_line_names_what_how_big_why_where_and_the_opt_out(clean_provision,
                                                               monkeypatch):
    rec = _Recorder()
    _pin_install(monkeypatch, chromium=provision.CHROMIUM_ABSENT)
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

    BOTH probe seams are made to RAISE rather than return False: a test that
    let either of them answer would go green on a developer box with chromium
    installed even if the opt-out had stopped working entirely, which is
    exactly how this fence was found unwitnessed. `playwright_importable` is
    pinned rather than guarded because the SKIPPED notice legitimately asks it
    which remedy to print (`_remedy`), and pinning it is what makes that line
    the same sentence on every install.
    """
    rec = _Recorder()
    monkeypatch.setenv(ENV, "true")
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)

    def must_not_run():
        raise AssertionError("probed for chromium after the operator opted out")

    monkeypatch.setattr(provision, "chromium_present", must_not_run)
    monkeypatch.setattr(provision, "probe_chromium", must_not_run)

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
    _pin_install(monkeypatch, chromium=provision.CHROMIUM_PRESENT)
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

    _pin_install(monkeypatch, chromium=provision.CHROMIUM_ABSENT)
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
    _pin_install(monkeypatch, chromium=provision.CHROMIUM_ABSENT)

    line = provision.announce_browser_autoinstall(console=rec.console)

    assert rec.lines == [line], (rec.lines, line)
    assert "chromium" in line.lower(), line
    assert ENV in line, line


def test_announce_says_nothing_when_chromium_is_present(clean_provision,
                                                        monkeypatch):
    rec = _Recorder()
    _pin_install(monkeypatch, chromium=provision.CHROMIUM_PRESENT)

    line = provision.announce_browser_autoinstall(console=rec.console)

    assert line is None, line
    assert rec.lines == [], rec.lines


def test_announce_reports_the_opt_out_without_probing_for_chromium(
        clean_provision, monkeypatch):
    """The opt-out is the operator's answer; asking playwright about it after
    they said no is work nobody asked for."""
    rec = _Recorder()
    monkeypatch.setenv(ENV, "true")
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)

    def must_not_run():
        raise AssertionError("probed for chromium after the operator opted out")

    monkeypatch.setattr(provision, "chromium_present", must_not_run)
    monkeypatch.setattr(provision, "probe_chromium", must_not_run)

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
    _pin_install(monkeypatch, chromium=provision.CHROMIUM_ABSENT)
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
    _pin_install(monkeypatch, chromium=provision.CHROMIUM_PRESENT)
    monkeypatch.setattr(provision, "_console_write", rec.console)
    monkeypatch.setattr(_stdlib_subprocess, "Popen", rec.popen)

    daemon_mod.start_daemon(
        vault_dir=str(vault), config=SimpleNamespace(), vault=SimpleNamespace(),
        port=_closed_port(), foreground=False, wait_timeout_s=0.0,
    )

    assert rec.events == ["spawn"], rec.events


# -- the positive control: the pins are pinning the CONSULTED seams ---------

def test_the_pinned_seams_are_the_ones_ensure_consults(clean_provision,
                                                       monkeypatch):
    """D8's own guard. `_pin_install` is only worth anything while the product
    still reads these two module attributes; the day it stops, every pinned
    test above would silently go back to answering from THIS machine -- green
    with the browser extra, and testing the wrong install without it.

    Patch each seam to raise: the product path has to carry the exception out.
    A seam nobody consults cannot.
    """
    rec = _Recorder()
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    def boom():
        raise _SeamNotConsulted("this seam was not consulted")

    monkeypatch.setattr(provision, "playwright_importable", boom)
    with pytest.raises(_SeamNotConsulted):
        provision.ensure_chromium_async(console=rec.console)

    monkeypatch.setattr(provision, "playwright_importable", lambda: True)
    monkeypatch.setattr(provision, "probe_chromium", boom)
    with pytest.raises(_SeamNotConsulted):
        provision.ensure_chromium_async(console=rec.console)

    assert "spawn" not in rec.events, rec.events


def test_the_pinned_seams_are_the_ones_announce_consults(clean_provision,
                                                         monkeypatch):
    """The same control for the PARENT's copy of the line -- the one the
    operator actually reads, and a separate decision frame with its own
    lookups."""
    rec = _Recorder()

    def boom():
        raise _SeamNotConsulted("this seam was not consulted")

    monkeypatch.setattr(provision, "playwright_importable", boom)
    with pytest.raises(_SeamNotConsulted):
        provision.announce_browser_autoinstall(console=rec.console)

    monkeypatch.setattr(provision, "playwright_importable", lambda: True)
    monkeypatch.setattr(provision, "probe_chromium", boom)
    with pytest.raises(_SeamNotConsulted):
        provision.announce_browser_autoinstall(console=rec.console)

    assert rec.lines == [], rec.lines


def test_the_pin_helper_pins_the_state_it_was_asked_for(clean_provision,
                                                        monkeypatch):
    """And the control's control: `_pin_install` asserts the verdict the real
    `probe_chromium` derived, so a pin that quietly produced UNKNOWN for every
    request -- which authorises no download and would make several tests above
    pass for the wrong reason -- cannot go unnoticed."""
    for state in (provision.CHROMIUM_PRESENT, provision.CHROMIUM_ABSENT,
                  provision.CHROMIUM_UNKNOWN):
        _pin_install(monkeypatch, chromium=state)
        assert provision.probe_chromium().state is state
        assert provision.playwright_importable() is True


def test_an_unanswerable_probe_announces_nothing_and_downloads_nothing(
        clean_provision, monkeypatch):
    """The third state, reachable now that the pin can express it: UNKNOWN is
    not ABSENT. Announcing on a non-answer is how a chromium that was already
    on disk got re-downloaded on every single daemon start."""
    rec = _Recorder()
    _pin_install(monkeypatch, chromium=provision.CHROMIUM_UNKNOWN)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    provision.ensure_chromium_async(console=rec.console)

    assert "spawn" not in rec.events, rec.events
    assert len(rec.lines) == 1, rec.lines
    assert "150 mb" not in rec.lines[0].lower(), rec.lines[0]
    assert "could not determine" in rec.lines[0].lower(), rec.lines[0]
