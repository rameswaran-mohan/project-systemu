"""D3 -- a Chromium probe that could not RUN never authorises a download.

THE DEFECT (e2e regression of v0.10.29, scratch install, Windows 11)
    With Chromium fully installed on disk --
    ``pwbrowsers2/chromium-1234/chrome-win64/chrome.exe`` present, roughly
    150 MB of it -- ``systemu daemon start`` reported:

        playwright_importable: True
        chromium_present: False
        _chromium_executable_path: None

    and announced, on the second start as on the first,

        "Chromium is not installed -- downloading it in the background now
         (roughly 150 MB)"

    The probe had not answered "no". It had FAILED: the Playwright sync API
    raised

        ImportError: DLL load failed while importing _greenlet:
        The filename or extension is too long

    and ``_chromium_executable_path`` wrapped the whole call in a bare
    ``except Exception: return None``, which ``chromium_present`` then read as
    "absent". Every cause of a failed probe -- a missing VC runtime, a greenlet
    ABI skew, a half-finished install, a path length -- presented as "not
    installed", forever, and re-triggered the download on every single start.

THE PROPERTY
    The probe is TRI-STATE: PRESENT / ABSENT / UNKNOWN(reason).

      * PRESENT  -- silent, as today.
      * ABSENT   -- playwright ANSWERED, and its answer was no. Announcement
                    and background install, as today.
      * UNKNOWN  -- the probe could not run. No announcement, no spawn, and one
                    plain line naming the reason, because an unanswerable probe
                    is not evidence of absence and must not spend the
                    operator's connection.

    This is DEC-32 in its ordinary form: the fail-closed answer rides in the
    VALUE the probe returns, so the frame that decides whether to download can
    tell "no" apart from "could not tell".

NO REAL PROBE AND NO REAL DOWNLOAD. The sync-API seam
(``provision._chromium_executable_path``) is monkeypatched at the module and
``subprocess.Popen`` with it; nothing here launches playwright, spawns an
install, or writes into the source tree.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import systemu.runtime.web.provision as provision


ENV = "SYSTEMU_SKIP_BROWSER_AUTOINSTALL"

#: The exact failure the regression witnessed, reproduced at the seam.
GREENLET_IMPORT_ERROR = ("DLL load failed while importing _greenlet: "
                         "The filename or extension is too long")


@pytest.fixture()
def clean_provision(monkeypatch):
    """A module that has not bootstrapped, with the opt-out unset and
    playwright importable -- the state the regression was witnessed in."""
    monkeypatch.setattr(provision, "_bootstrapped", False)
    monkeypatch.setattr(provision, "_install_waiter", None, raising=False)
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(provision, "playwright_importable", lambda: True)
    return provision


class _Recorder:
    """The ORDER of console lines and spawns."""

    def __init__(self):
        self.events = []
        self.lines = []

    def console(self, line):
        self.events.append("console")
        self.lines.append(line)

    def popen(self, *args, **kwargs):
        self.events.append("spawn")
        return SimpleNamespace(pid=4242, wait=lambda *a, **k: 0)


def _seam_raises(monkeypatch, exc):
    """The sync API fails the way the operator's box failed."""
    def boom():
        raise exc
    monkeypatch.setattr(provision, "_chromium_executable_path", boom)


def _seam_returns(monkeypatch, value):
    monkeypatch.setattr(provision, "_chromium_executable_path", lambda: value)


# --------------------------------------------------------------------------- #
# The probe itself: three states, not two
# --------------------------------------------------------------------------- #

def test_a_seam_that_raises_is_UNKNOWN_and_carries_its_reason(monkeypatch):
    """THE repro. The sync API raised; the module returned None and called it
    "not installed"."""
    _seam_raises(monkeypatch, ImportError(GREENLET_IMPORT_ERROR))

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_UNKNOWN, verdict
    assert "greenlet" in verdict.reason.lower(), verdict.reason
    assert "importerror" in verdict.reason.lower(), verdict.reason


def test_a_seam_that_answers_with_a_real_path_is_PRESENT(monkeypatch, tmp_path):
    exe = tmp_path / "pwbrowsers" / "chromium-1234" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="ascii")
    _seam_returns(monkeypatch, str(exe))

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_PRESENT, verdict
    assert verdict.reason == "", verdict.reason


def test_a_seam_that_answers_none_is_ABSENT(monkeypatch):
    """Playwright ANSWERED, and the answer was no. That is the one state a
    download is allowed to follow from."""
    _seam_returns(monkeypatch, None)

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_ABSENT, verdict


def test_a_path_playwright_names_but_that_is_not_on_disk_is_ABSENT(monkeypatch,
                                                                   tmp_path):
    """The other half of ABSENT: an answer that names a binary nothing put
    there. The probe ran; the browser is missing."""
    _seam_returns(monkeypatch, str(tmp_path / "never-installed" / "chrome.exe"))

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_ABSENT, verdict


def test_the_three_states_are_distinct(monkeypatch):
    """A mutation that collapses UNKNOWN into ABSENT has to make one of these
    three constants equal to another, which is louder than a quiet fallthrough.
    """
    states = {provision.CHROMIUM_PRESENT, provision.CHROMIUM_ABSENT,
              provision.CHROMIUM_UNKNOWN}
    assert len(states) == 3, states


def test_the_unknown_reason_is_ascii(monkeypatch):
    """DEC-32c: this reason is printed at daemon start, on a console that may
    be cp1252. A reason it cannot encode is a UnicodeEncodeError in place of
    the notice."""
    _seam_raises(monkeypatch, RuntimeError("browser at C:\\pw\\\u2713\\chrome"))

    verdict = provision.probe_chromium()

    assert verdict.state == provision.CHROMIUM_UNKNOWN, verdict
    verdict.reason.encode("ascii")


# --------------------------------------------------------------------------- #
# The decision: an UNKNOWN never announces and never spawns
# --------------------------------------------------------------------------- #

def test_an_unanswerable_probe_neither_announces_nor_spawns(clean_provision,
                                                            monkeypatch):
    """THE defect, at the surface that has the operator's connection: a false
    "not installed" pulled 150 MB it did not need on every start."""
    rec = _Recorder()
    _seam_raises(monkeypatch, ImportError(GREENLET_IMPORT_ERROR))
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)

    assert "spawn" not in rec.events, rec.events
    assert len(rec.lines) == 1, rec.lines
    line = rec.lines[0]
    assert "150 mb" not in line.lower(), line
    assert provision.autoinstall_notice() not in rec.lines, rec.lines


def test_the_unknown_line_says_it_could_not_tell_and_why(clean_provision,
                                                         monkeypatch):
    """The operator has to be able to act on it, so the line names the reason
    the probe gave and does not claim the browser is missing."""
    rec = _Recorder()
    _seam_raises(monkeypatch, ImportError(GREENLET_IMPORT_ERROR))
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    provision.ensure_chromium_async(console=rec.console)

    line = rec.lines[0]
    assert "could not determine whether chromium is installed" in line.lower(), line
    assert "not downloading it" in line.lower(), line
    assert "until the probe can run" in line.lower(), line
    assert "greenlet" in line.lower(), line
    line.encode("ascii")


def test_the_parents_announcement_is_silent_about_a_download_it_cannot_justify(
        clean_provision, monkeypatch):
    """`daemon start` announces from the PARENT -- that is the copy the
    operator reads, and it was the one making the false claim on every start.
    """
    rec = _Recorder()
    _seam_raises(monkeypatch, ImportError(GREENLET_IMPORT_ERROR))

    line = provision.announce_browser_autoinstall(console=rec.console)

    assert rec.lines == [line], (rec.lines, line)
    assert "150 mb" not in line.lower(), line
    assert "could not determine whether chromium is installed" in line.lower(), line


def test_a_present_chromium_is_still_silent(clean_provision, monkeypatch,
                                            tmp_path):
    """The control on the PRESENT side: nothing to download is nothing to
    say."""
    exe = tmp_path / "chrome.exe"
    exe.write_text("", encoding="ascii")
    rec = _Recorder()
    _seam_returns(monkeypatch, str(exe))
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    provision.ensure_chromium_async(console=rec.console)
    line = provision.announce_browser_autoinstall(console=rec.console)

    assert rec.events == [], rec.events
    assert line is None, line


def test_an_answered_absence_still_announces_and_still_spawns(clean_provision,
                                                              monkeypatch):
    """The control on the ABSENT side, and the mutation's other half: routing
    the decision through the tri-state must not have turned the auto-install
    off. Playwright answered "no", so the download is honest."""
    rec = _Recorder()
    _seam_returns(monkeypatch, None)
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)
    monkeypatch.setattr(provision, "_publish_banner", lambda: None)

    provision.ensure_chromium_async(console=rec.console)

    assert rec.events == ["console", "spawn"], rec.events
    assert "150 mb" in rec.lines[0].lower(), rec.lines[0]


def test_the_opt_out_still_wins_over_an_unanswerable_probe(clean_provision,
                                                           monkeypatch):
    """The operator's answer is the operator's answer; a probe that could not
    run does not get to replace their line with a different one."""
    rec = _Recorder()
    monkeypatch.setenv(ENV, "true")
    _seam_raises(monkeypatch, ImportError(GREENLET_IMPORT_ERROR))
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    provision.ensure_chromium_async(console=rec.console)

    assert "spawn" not in rec.events, rec.events
    assert len(rec.lines) == 1, rec.lines
    assert ENV in rec.lines[0], rec.lines[0]


def test_a_missing_playwright_still_reads_as_missing_playwright(clean_provision,
                                                                monkeypatch):
    """Ordering: "playwright is not here" is a better answer than "the probe
    could not run", and it is the one with a remedy attached."""
    rec = _Recorder()
    monkeypatch.setattr(provision, "playwright_importable", lambda: False)
    _seam_raises(monkeypatch, ImportError(GREENLET_IMPORT_ERROR))
    monkeypatch.setattr(provision.subprocess, "Popen", rec.popen)

    provision.ensure_chromium_async(console=rec.console)

    assert "spawn" not in rec.events, rec.events
    assert len(rec.lines) == 1, rec.lines
    assert 'pip install "systemu[browser]"' in rec.lines[0], rec.lines[0]


# --------------------------------------------------------------------------- #
# The bool the old callers use is DERIVED, never the thing that decides
# --------------------------------------------------------------------------- #

def test_chromium_present_keeps_its_name_and_answers_for_the_two_answerable_states(
        monkeypatch, tmp_path):
    exe = tmp_path / "chrome.exe"
    exe.write_text("", encoding="ascii")
    _seam_returns(monkeypatch, str(exe))
    assert provision.chromium_present() is True
    _seam_returns(monkeypatch, None)
    assert provision.chromium_present() is False


def test_the_decision_reads_the_tri_state_not_the_bool():
    """Reachability pin. Delete the tri-state consumption from the two decision
    sites and this goes red -- which is what a test asserting only "no spawn"
    cannot tell apart from an implementation that stopped spawning at all.
    """
    import inspect

    for fn in (provision.ensure_chromium_async,
               provision.announce_browser_autoinstall):
        source = inspect.getsource(fn)
        assert "probe_chromium" in source, (
            "{} no longer consumes the tri-state probe, so an unanswerable "
            "probe can authorise a download again:\n{}".format(
                fn.__name__, source))
        assert provision.CHROMIUM_UNKNOWN in source, (
            "{} does not name the UNKNOWN state, so it cannot be treating it "
            "as its own case:\n{}".format(fn.__name__, source))
