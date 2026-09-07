"""N3 + N7 -- every `daemon status` verdict names the vault and the port's
provenance, and the path it names is a path an operator can copy.

WITNESSED DEFECTS (v0.10.28, scratch install, an 80-column console)

N3  The READY panel named neither.  ``scheduler/daemon.py`` mints both facts --
    a ``vault_root`` field and a port-provenance clause -- but the CLI only ever
    reached them through ``status["reason"]``, which the renderer prints on the
    Not-running and Starting panels and NOT on Ready.  The observed Ready panel
    was PID + URL + build line: on a machine with two vaults, "Ready" for which
    one was unanswerable, and the port could still be the built-in guess.

    ``grep -n vault_root systemu/interface/cli_commands.py`` found nothing.

N7  The Not-running panel WRAPPED the absolute vault path across Rich panel
    lines::

        no live daemon process and nothing is accepting connections on
        127.0.0.1:8765 (port 8765 is the built-in default); vault: D:\\...
        \\worktrees\\objectiv
        e-wescoff-1b02d7\\systemu\\vault; if the daemon was started on ...

    The `roots` group already carries the ruling this breaks, in its own source:
    *a path wrapped at column 80 is not a path*.  A path split across two lines
    cannot be pasted, cannot be searched for, and reads as two directories.

THE PROPERTY PINNED HERE
    For EVERY verdict -- Ready, Starting, Not running -- the rendered output
    carries the operating vault root on ONE unwrapped line and the port's
    provenance clause, both taken from the MINT's projection (``vault_root`` and
    the minted provenance note in the status dict) rather than re-derived by the
    renderer.  The panel keeps the verdict, the PID and the URL; the path-shaped
    facts go outside it, where nothing folds them.

NO REAL DAEMON IS STARTED HERE -- the status mint is monkeypatched, and every
path lives under ``tmp_path``.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import systemu.scheduler.daemon as daemon_mod
from systemu.interface import cli_commands as cc


#: The console width the defect was witnessed at.
_COLUMNS = "80"

#: Rich draws panels with UNICODE box-drawing characters; they are not content.
#: ASCII hyphens and pipes are deliberately NOT in this set: a real vault path
#: carries hyphens (`objective-wescoff-1b02d7`), and stripping them would splice
#: the two halves of a folded path back together -- hiding the very defect the
#: N7 tests below exist to witness.
_BOX = set("\u250c\u2510\u2514\u2518\u2500\u2502\u256d\u256e\u2570\u256f\u251c\u2524")


def _deep_vault(tmp_path) -> str:
    """A vault root long enough that an 80-column panel MUST fold it.

    The witnessed path was 84 characters inside the panel body; anything that
    only fits by accident cannot witness the defect.
    """
    root = tmp_path
    for part in ("Antigravity", "Project_systemu_pro", "dot_claude_worktrees",
                 "objective-wescoff-1b02d7", "systemu", "vault"):
        root = root / part
    root.mkdir(parents=True, exist_ok=True)
    text = str(root)
    assert len(text) > 76, (
        f"fixture premise: the path must not fit an 80-column panel ({len(text)})")
    return text


def _status(vault_root: str, *, ready: bool, process_alive: bool) -> dict:
    """One status dict in the exact shape ``daemon.get_status`` projects."""
    return {
        "running": ready,
        "ready": ready,
        "pid": 4242,
        "process_alive": process_alive,
        "host": "127.0.0.1",
        "port": 8765,
        "url": "http://127.0.0.1:8765",
        "reason": "a reason line the panel may or may not print",
        "port_source": "default",
        "vault_root": vault_root,
        "port_provenance": daemon_mod.port_provenance_note(8765, "default"),
        "daemon_version": "0.10.28",
        "daemon_path": "somewhere",
        "cli_version": "0.10.28",
        "cli_path": "somewhere",
        "build_match": True,
        "build_note": "daemon and CLI are the same build",
    }


#: The three verdicts, by the flags that select the renderer's branch.
_VERDICTS = {
    "ready": dict(ready=True, process_alive=True),
    "starting": dict(ready=False, process_alive=True),
    "not-running": dict(ready=False, process_alive=False),
}


def _run(monkeypatch, tmp_path, verdict: str) -> tuple[str, str]:
    """Render `daemon status` for one verdict.  Returns (output, vault_root)."""
    vault_root = _deep_vault(tmp_path)
    monkeypatch.setenv("COLUMNS", _COLUMNS)
    monkeypatch.setattr(
        daemon_mod, "get_status",
        lambda vault_dir, **kw: _status(vault_root, **_VERDICTS[verdict]))

    res = CliRunner().invoke(
        cc.daemon_status, [],
        obj={"config": SimpleNamespace(vault_dir=vault_root),
             "vault": SimpleNamespace()},
        env={"COLUMNS": _COLUMNS},
    )
    assert res.exit_code == 0, res.output
    return res.output, vault_root


def _unwrapped_lines(output: str) -> list[str]:
    """Output lines with any panel border characters stripped off the ends."""
    out = []
    for line in output.splitlines():
        out.append("".join(ch for ch in line if ch not in _BOX).strip())
    return out


# --------------------------------------------------------------------------- #
# N3 -- every verdict names the vault and where the port number came from
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_every_verdict_names_the_operating_vault_root(verdict, tmp_path,
                                                      monkeypatch):
    """Ready included.  "Ready" for WHICH vault is the question a two-vault
    machine actually asks, and the Ready panel never answered it."""
    output, vault_root = _run(monkeypatch, tmp_path, verdict)
    assert vault_root in output, (
        f"the {verdict} verdict does not name the operating vault:\n{output}")


@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_every_verdict_names_the_ports_provenance(verdict, tmp_path, monkeypatch):
    """8765 with no daemon record is a GUESS this process made.  Printed in the
    same voice as a number the operator chose, it is a misleading fact."""
    output, _root = _run(monkeypatch, tmp_path, verdict)
    note = daemon_mod.port_provenance_note(8765, "default")
    assert note in output, (
        f"the {verdict} verdict does not disclose where the port came from "
        f"(wanted {note!r}):\n{output}")


# --------------------------------------------------------------------------- #
# N7 -- the path is a path: ONE line, unwrapped, at 80 columns
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_the_vault_path_is_never_folded_across_lines(verdict, tmp_path,
                                                     monkeypatch):
    """The `roots` group's own rule, applied to the surface that broke it: a
    path wrapped at column 80 is not a path."""
    output, vault_root = _run(monkeypatch, tmp_path, verdict)
    lines = _unwrapped_lines(output)
    assert any(vault_root in line for line in lines), (
        f"the {verdict} verdict folded the vault path across panel lines -- it "
        f"cannot be pasted or searched for:\n{output}")


@pytest.mark.parametrize("verdict", sorted(_VERDICTS))
def test_the_path_line_sits_outside_the_panel(verdict, tmp_path, monkeypatch):
    """Structural, not cosmetic: a long path inside a Rich panel is folded by
    the panel, so keeping the line unwrapped means keeping it OUT of one."""
    output, vault_root = _run(monkeypatch, tmp_path, verdict)
    carrier = [ln for ln in output.splitlines() if vault_root in ln]
    assert carrier, f"no line carries the vault path:\n{output}"
    for line in carrier:
        assert not (set(line) & _BOX), (
            f"the vault path is inside a panel, which folds it:\n{line!r}")


# --------------------------------------------------------------------------- #
# N3 -- the facts come from the MINT, never from a second derivation
# --------------------------------------------------------------------------- #

def test_daemon_status_consumes_vault_root_from_the_projection():
    """Reachability pin.  Remove the consumption and this goes red.

    The defect was not a missing fact -- ``get_status`` has projected
    ``vault_root`` since v0.10.27.  It was a renderer that never read it, which
    a test asserting only "the path appears somewhere" cannot tell apart from a
    renderer that resolved the vault a second, independent way.
    """
    source = inspect.getsource(cc.daemon_status.callback)
    assert "vault_root" in source, (
        "`daemon_status` no longer consumes the mint's `vault_root`:\n" + source)
    assert "resolve_vault_root" not in source, (
        "`daemon_status` re-derives the vault root instead of consuming the "
        "projection -- two derivations is the defect, not the fix:\n" + source)


def test_the_provenance_note_is_projected_by_the_mint_not_reformatted_here():
    """The clause the renderer prints is the clause the mint made.  A renderer
    that re-formats it can drift from the one folded into ``reason``."""
    source = inspect.getsource(cc.daemon_status.callback)
    assert "port_provenance" in source, (
        "`daemon_status` does not consume the minted provenance note:\n" + source)


def test_get_status_projects_the_provenance_note(tmp_path, monkeypatch):
    """A fact the mint carries but the dict drops is a fact no surface reaches."""
    vault = tmp_path / "home" / "vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault))
    monkeypatch.delenv("SYSTEMU_DASHBOARD_PORT", raising=False)

    status = daemon_mod.get_status(str(vault), timeout=0.2)

    assert status["port_provenance"] == daemon_mod.port_provenance_note(
        status["port"], status["port_source"])
    assert status["port_provenance"], "an empty provenance note discloses nothing"


def test_the_rendered_status_is_ascii_apart_from_the_panel_itself(tmp_path,
                                                                  monkeypatch):
    """The lines added here are verdict-carrying text (DEC-32c applies), so they
    must survive a cp1252 console even though Rich's own box art need not."""
    output, vault_root = _run(monkeypatch, tmp_path, "not-running")
    carriers = [ln for ln in output.splitlines()
                if vault_root in ln or "port 8765" in ln]
    assert carriers, f"nothing to check -- no line carries either fact:\n{output}"
    for line in carriers:
        line.encode("ascii")
