"""P3 -- the PRESENT branch of the Chromium provision left no witness.

THE DEFECT
    `ensure_chromium_async` logs on every branch it can reach EXCEPT the one a
    healthy install takes:

        opted out   -> INFO "browser auto-install skipped (env opt-out)"
        no wheel    -> INFO "playwright is not installed ..."
        UNKNOWN     -> WARNING "chromium presence is unknown (...)"
        ABSENT      -> INFO "chromium missing -- spawning background install"
        PRESENT     -> (nothing at all)

    So the log a support question arrives with cannot distinguish "the probe
    said PRESENT and there was nothing to do" from "the provision hook never
    ran". Those have different causes and different remedies, and silence is
    the reading an operator gets for both. This is the same shape as the
    module's own D3 defect one floor down -- a state inferred from an absence
    rather than witnessed -- at the log surface rather than the console.

THE PROPERTY
    A PRESENT verdict is WITNESSED: one INFO line naming the executable that
    was found and saying that nothing is being downloaded. Console silence is
    deliberate and unchanged -- nothing to download is nothing to interrupt the
    operator with -- so this is a LOG line, not a console line.

NOTHING HERE LAUNCHES A DRIVER OR DOWNLOADS A BROWSER.
"""
from __future__ import annotations

import logging

import pytest

import systemu.runtime.web.provision as provision


ENV = "SYSTEMU_SKIP_BROWSER_AUTOINSTALL"

#: A path that is not on this machine. It never has to exist: the probe is
#: replaced at its seam, and the point is that the LINE carries whatever the
#: probe reported.
FOUND_AT = "/fake-browsers/chromium-9911/chrome-linux/chrome"


@pytest.fixture()
def clean_provision(monkeypatch):
    """A module that has not bootstrapped, opt-out unset, playwright present."""
    monkeypatch.setattr(provision, "_bootstrapped", False)
    monkeypatch.setattr(provision, "_install_waiter", None, raising=False)
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)
    return provision


class _Recorder:
    def __init__(self):
        self.events = []
        self.lines = []

    def console(self, line):
        self.events.append("console")
        self.lines.append(line)

    def popen(self, *args, **kwargs):                # pragma: no cover - guard
        self.events.append("spawn")
        raise AssertionError("a PRESENT chromium must never spawn an install")


def _present_at(monkeypatch, path):
    """The probe answers PRESENT, and names where."""
    monkeypatch.setattr(
        provision, "probe_chromium",
        lambda: provision.ChromiumVerdict(provision.CHROMIUM_PRESENT,
                                          path=path))


def _provision_records(caplog, level=logging.INFO):
    return [r for r in caplog.records
            if r.name == provision.logger.name and r.levelno >= level]


def test_a_present_chromium_is_named_in_the_log(clean_provision, monkeypatch,
                                                caplog):
    """THE PIN. One line, the path in it, and the claim that nothing is coming
    down the operator's connection."""
    rec = _Recorder()
    _present_at(monkeypatch, FOUND_AT)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    caplog.set_level(logging.INFO, logger=provision.logger.name)

    provision.ensure_chromium_async(console=rec.console)

    messages = [r.getMessage() for r in _provision_records(caplog)]
    named = [m for m in messages if FOUND_AT in m]
    assert len(named) == 1, messages
    line = named[0]
    assert "chromium present at" in line.lower(), line
    assert "nothing to download" in line.lower(), line
    line.encode("ascii")


def test_the_witness_is_a_log_line_and_not_a_console_line(clean_provision,
                                                          monkeypatch, caplog):
    """The control that keeps the fix from becoming noise: `daemon start` stays
    silent on a healthy install. A line on every start is how a real notice gets
    scrolled past -- which is the defect this module closed one release ago."""
    rec = _Recorder()
    _present_at(monkeypatch, FOUND_AT)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    caplog.set_level(logging.INFO, logger=provision.logger.name)

    provision.ensure_chromium_async(console=rec.console)

    assert rec.events == [], rec.events
    assert rec.lines == [], rec.lines


def test_a_present_chromium_still_bootstraps_and_never_spawns(clean_provision,
                                                              monkeypatch):
    """The behaviour under the new line is the behaviour that was there: no
    install, and the module is done. `_Recorder.popen` raises rather than
    records, so a spawn cannot pass as a quiet event."""
    rec = _Recorder()
    _present_at(monkeypatch, FOUND_AT)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    provision.ensure_chromium_async(console=rec.console)

    assert provision._bootstrapped is True


def test_a_present_chromium_whose_path_could_not_be_read_still_says_so(
        clean_provision, monkeypatch, caplog):
    """A witness the probe could not fill in costs the line its DETAIL, never
    the line itself: "chromium present, path unknown" is still the answer to
    "did the provision hook run", which is what the silence hid."""
    rec = _Recorder()
    _present_at(monkeypatch, "")
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    caplog.set_level(logging.INFO, logger=provision.logger.name)

    provision.ensure_chromium_async(console=rec.console)

    messages = [r.getMessage() for r in _provision_records(caplog)]
    lines = [m for m in messages if "chromium present" in m.lower()]
    assert len(lines) == 1, messages
    assert "nothing to download" in lines[0].lower(), lines[0]
    lines[0].encode("ascii")


def test_a_non_ascii_install_path_is_still_loggable(clean_provision,
                                                    monkeypatch, caplog):
    """DEC-32c. This line reaches daemon.log and, on a foreground start, a
    console that may be cp1252; a Windows profile name is exactly where a
    non-ASCII character comes from. The path is spelled ASCII before it goes
    out, so the line is a line rather than a UnicodeEncodeError."""
    rec = _Recorder()
    _present_at(monkeypatch, "C:\\Users\\Ren\u00e9\\ms-playwright\\chrome.exe")
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    caplog.set_level(logging.INFO, logger=provision.logger.name)

    provision.ensure_chromium_async(console=rec.console)

    messages = [r.getMessage() for r in _provision_records(caplog)]
    lines = [m for m in messages if "chromium present" in m.lower()]
    assert len(lines) == 1, messages
    lines[0].encode("ascii")


def test_the_other_branches_keep_their_own_witnesses(clean_provision,
                                                     monkeypatch, caplog):
    """The reason this finding is worth a line at all: every OTHER branch
    already leaves one, so PRESENT was the single state the log could not
    distinguish from "the hook never ran"."""
    rec = _Recorder()
    monkeypatch.setattr(
        provision, "probe_chromium",
        lambda: provision.ChromiumVerdict(provision.CHROMIUM_UNKNOWN,
                                          "ImportError: nope"))
    caplog.set_level(logging.INFO, logger=provision.logger.name)

    provision.ensure_chromium_async(console=rec.console)

    messages = [r.getMessage() for r in _provision_records(caplog)]
    assert any("unknown" in m.lower() for m in messages), messages
    assert not any("chromium present" in m.lower() for m in messages), messages
