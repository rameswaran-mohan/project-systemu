"""D2 -- the daemon build line is a PATH line, and a path is never wrapped.

THE DEFECT (e2e regression of v0.10.29)
    ``_print_daemon_build`` used ``console.print`` -- Rich, which folds a
    paragraph at the console width wherever the break happens to land. On an
    80-column terminal the witnessed line

        same build on both sides: systemu 0.10.29 from C:\\...\\site-packages

    came out in four fragments. Two lines below it in the same file,
    ``_print_daemon_where`` deliberately uses ``click.echo`` for exactly this
    reason, with the ruling written out in its docstring: *a path wrapped at
    column 80 is not a path* -- it can be neither pasted nor searched for.

    The build line is the one that matters MID-UPGRADE. ``!! build mismatch``
    names the two installations that disagree, and the whole point of naming
    them is that the operator can go and look at them; a folded path is a
    diagnosis they cannot act on.

THE PROPERTY
    Every variant of the build line -- agreement, mismatch, and the UNVERIFIED
    third state -- is emitted unwrapped, one line per path, on both surfaces
    that print it (``daemon status`` and ``daemon start``).

NO REAL DAEMON. The readiness mint (``daemon.get_status`` / ``start_daemon``)
is monkeypatched at its module; nothing is spawned and no vault is written.
All paths below are synthetic.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.interface.cli_commands as cc
import systemu.scheduler.daemon as daemon_mod


#: The width the regression was witnessed at.
_COLUMNS = "80"

#: A synthetic absolute path well past 150 characters. Long enough that no
#: plausible terminal width can carry it AND the sentence around it, which is
#: the only way to tell an unwrapped line apart from a lucky short one.
_LONG_PATH = ("C:\\opt\\systemu-runtime\\environments\\production-3\\"
              "very-long-directory-segment-for-the-regression\\"
              "another-long-directory-segment\\lib\\site-packages\\systemu")

_NOTES = {
    "mismatch": ("BUILD SKEW: the daemon is executing systemu 0.10.28 from "
                 + _LONG_PATH + ", but this CLI is systemu 0.10.29."),
    "agreement": ("same build on both sides: systemu 0.10.29 from "
                  + _LONG_PATH),
    "unverified": ("the daemon process did not record which systemu build it "
                   "loaded; this CLI is systemu 0.10.29 from " + _LONG_PATH),
}

_MATCH = {"mismatch": False, "agreement": True, "unverified": None}


def _status(*, build_match, build_note, vault_root):
    """A Ready verdict, projected the way `get_status` projects one."""
    return {
        "running": True,
        "ready": True,
        "pid": 4242,
        "process_alive": True,
        "host": "127.0.0.1",
        "port": 8765,
        "url": "http://127.0.0.1:8765",
        "reason": "accepting connections on http://127.0.0.1:8765",
        "vault_root": vault_root,
        "port_provenance": daemon_mod.port_provenance_note(8765, "default"),
        "daemon_version": "0.10.28",
        "daemon_path": _LONG_PATH,
        "cli_version": "0.10.29",
        "cli_path": "somewhere",
        "build_match": build_match,
        "build_note": build_note,
    }


def _run_status(monkeypatch, tmp_path, variant):
    vault_root = str(tmp_path / "vault")
    monkeypatch.setenv("COLUMNS", _COLUMNS)
    monkeypatch.setattr(
        daemon_mod, "get_status",
        lambda vault_dir, **kw: _status(build_match=_MATCH[variant],
                                        build_note=_NOTES[variant],
                                        vault_root=vault_root))
    res = CliRunner().invoke(
        cc.daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_root),
             "vault": SimpleNamespace()},
        env={"COLUMNS": _COLUMNS},
    )
    assert res.exit_code == 0, res.output
    return res.output


# --------------------------------------------------------------------------- #
# The line itself, on `daemon status`
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("variant", sorted(_NOTES))
def test_the_build_line_carries_the_whole_path_on_one_line(variant, tmp_path,
                                                           monkeypatch):
    """THE repro, at 80 columns: the witnessed line arrived in four
    fragments."""
    output = _run_status(monkeypatch, tmp_path, variant)

    assert _LONG_PATH in output, (
        "the build path is not in the output at all:\n" + output)
    carrier = [ln for ln in output.splitlines() if _LONG_PATH in ln]
    assert carrier, (
        "the {} build line folded the path across lines -- it can be neither "
        "pasted nor searched for:\n{}".format(variant, output))


@pytest.mark.parametrize("variant", sorted(_NOTES))
def test_the_whole_build_note_is_one_line(variant, tmp_path, monkeypatch):
    """Not just the path: the SENTENCE is one line. A note broken between the
    version and the path it belongs to reads as two facts."""
    output = _run_status(monkeypatch, tmp_path, variant)
    note = _NOTES[variant]

    assert any(note in ln for ln in output.splitlines()), (
        "the {} build note is folded:\n{}".format(variant, output))


@pytest.mark.parametrize("variant", sorted(_NOTES))
def test_the_build_line_is_ascii(variant, tmp_path, monkeypatch):
    """DEC-32c: a verdict-carrying line a cp1252 console cannot encode is a
    UnicodeEncodeError in place of the verdict."""
    output = _run_status(monkeypatch, tmp_path, variant)
    carrier = [ln for ln in output.splitlines() if _LONG_PATH in ln]
    assert carrier, output
    for line in carrier:
        line.encode("ascii")


def test_a_mismatch_is_still_loud(tmp_path, monkeypatch):
    """Unwrapping must not have cost the mismatch its marker. `!!` is the
    ASCII spelling of the warning the coloured glyph used to carry."""
    output = _run_status(monkeypatch, tmp_path, "mismatch")
    carrier = [ln for ln in output.splitlines() if _NOTES["mismatch"] in ln]
    assert carrier, output
    assert any("!!" in ln for ln in carrier), (
        "a build MISMATCH is printed with no marker at all:\n" + output)


def test_agreement_carries_no_alarm_marker(tmp_path, monkeypatch):
    """The control: `!!` on every start is `!!` nobody reads."""
    output = _run_status(monkeypatch, tmp_path, "agreement")
    carrier = [ln for ln in output.splitlines() if _NOTES["agreement"] in ln]
    assert carrier, output
    assert not any("!!" in ln for ln in carrier), (
        "agreement is marked as a problem:\n" + output)


def test_an_unverified_build_is_marked_too(tmp_path, monkeypatch):
    """UNVERIFIED is its own state and never agreement: a daemon that recorded
    no build IS an older/other build, which is the skew itself."""
    output = _run_status(monkeypatch, tmp_path, "unverified")
    carrier = [ln for ln in output.splitlines() if _NOTES["unverified"] in ln]
    assert carrier, output
    assert any("!!" in ln for ln in carrier), (
        "an UNVERIFIED build is rendered as agreement:\n" + output)


# --------------------------------------------------------------------------- #
# The same line on `daemon start`, which is where a mid-upgrade skew shows up
# --------------------------------------------------------------------------- #

def test_daemon_start_prints_the_build_line_unwrapped(tmp_path, monkeypatch):
    """`daemon start` is the surface an operator reads mid-upgrade, and the
    skew line there names the two installations that disagree."""
    monkeypatch.setenv("COLUMNS", _COLUMNS)
    monkeypatch.setattr("systemu.runtime.optional_deps.missing_groups",
                        lambda groups: ())
    monkeypatch.setattr("sharing_on.setup_flow.key_present", lambda: True)

    verdict = daemon_mod.DaemonReadiness(
        ready=True, pid=4242, process_alive=True, host="127.0.0.1", port=8765,
        reason="accepting connections on http://127.0.0.1:8765",
        build_match=False, build_note=_NOTES["mismatch"],
    )
    monkeypatch.setattr(daemon_mod, "start_daemon",
                        lambda **kw: verdict)

    res = CliRunner().invoke(
        cc.daemon_start, [],
        obj={"config": SimpleNamespace(vault_dir=str(tmp_path / "vault")),
             "vault": SimpleNamespace()},
        env={"COLUMNS": _COLUMNS},
    )

    assert res.exit_code == 0, res.output
    assert any(_NOTES["mismatch"] in ln for ln in res.output.splitlines()), (
        "the build line folded on `daemon start`:\n" + res.output)


# --------------------------------------------------------------------------- #
# Reachability: the unwrapped writer is the one this function uses
# --------------------------------------------------------------------------- #

def test_the_build_printer_uses_the_unwrapping_writer():
    """Structural, not cosmetic. `console.print` folds at the console width by
    design, so "unwrapped" means "not printed through Rich" -- the same reason
    `_print_daemon_where` next door is line-oriented.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(cc._print_daemon_build)))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name):
                called.add("{}.{}".format(owner.id, node.func.attr))

    assert "console.print" not in called, (
        "the build line is printed through Rich again, which folds it at the "
        "console width: " + repr(sorted(called)))
    assert called & {"click.echo", "click.secho"}, (
        "the build line is not printed with a writer that leaves it "
        "unwrapped: " + repr(sorted(called)))
