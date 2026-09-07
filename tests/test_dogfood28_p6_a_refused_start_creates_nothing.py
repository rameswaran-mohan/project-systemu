"""P6 -- a REFUSED `daemon start` creates nothing.

THE WITNESS (0.10.30 dogfood)
-----------------------------
On a bare `pip install systemu` -- no `[dashboard]` extra -- `systemu daemon
start` correctly refuses in under a second and prints the command that installs
the extra. It also leaves a full vault tree behind in whatever directory it was
run in, because `_get_vault_and_config(ctx)` ran on the line ABOVE the gate:
`Config.from_env()` then `open_vault(cfg)`, which mints the whole layout.

That is the worst possible first impression of a program whose entire pitch is
that it does not act without consent: the operator asked for something, was
told it could not be done, and got directories they did not ask for in a folder
they may only have been passing through. It is also the D-shape of the vault
root fence (DEC-32), which refuses rather than writing outside the operating
home -- a refusal that WRITES is not a refusal.

THE RULING
    The gate runs FIRST. A refusal creates nothing.

The order is the whole fix: the dashboard-extra gate reads no config and needs
no vault -- it asks one question of the current interpreter -- so there was
never a reason for it to run second.

FAKED AT THE ONE PROBE. `optional_deps.is_installed` is the single place
availability enters the process (that module's own rule: "Do not add
`try: import nicegui` anywhere"), so faking it there is faking it everywhere,
and the command under test is not stubbed at all.
"""
from __future__ import annotations

import inspect

import pytest
from click.testing import CliRunner

import systemu.interface.cli_commands as cc
from systemu.runtime import optional_deps as od


#: Environment that would send the vault somewhere other than the cwd. Cleared
#: so the assertion below is about THIS directory and nothing else.
_REDIRECTS = ("SYSTEMU_VAULT_DIR", "SYSTEMU_HOME", "SYSTEMU_STORAGE")


@pytest.fixture()
def bare_install(monkeypatch, tmp_path):
    """A cwd with nothing in it, and an interpreter with no dashboard extra."""
    real_is_installed = od.is_installed

    def _without_nicegui(package):
        if od.canonical(package) == "nicegui":
            return False
        return real_is_installed(package)

    monkeypatch.setattr(od, "is_installed", _without_nicegui)
    for name in _REDIRECTS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    assert list(tmp_path.iterdir()) == [], "fixture premise: the cwd is empty"
    return tmp_path


def _run_start(cwd):
    """`daemon start` with an EMPTY context -- so `_get_vault_and_config` really
    runs if the command reaches it, which is the thing under test."""
    return CliRunner().invoke(cc.daemon_start, [], obj={}, env={"COLUMNS": "80"})


# --------------------------------------------------------------------------- #
# the refusal, and what it leaves behind
# --------------------------------------------------------------------------- #

def test_a_refused_start_creates_nothing_in_the_working_directory(bare_install):
    """THE REPRO. The refusal was right; the tree it left was not."""
    res = _run_start(bare_install)

    assert res.exit_code == 1, res.output      # DEC-41: not started != success
    leftovers = sorted(p.name for p in bare_install.iterdir())
    assert leftovers == [], (
        "a refused `daemon start` created " + repr(leftovers)
        + " in the operator's working directory:\n" + res.output)


def test_the_refusal_still_says_what_it_says(bare_install):
    """The gate's copy is unchanged by the reordering -- it is the one useful
    thing on this screen."""
    res = _run_start(bare_install)

    assert "web dashboard is not installed" in res.output, res.output
    assert "pip install" in res.output, res.output
    assert [ln for ln in res.output.splitlines()
            if "systemu[dashboard]" in ln], (
        "the install command was folded across lines:\n" + res.output)


def test_nothing_is_created_anywhere_under_the_cwd(bare_install):
    """Not just "no vault directory": no file either. A refusal that writes a
    log, a lock or a marker is still a refusal that writes."""
    res = _run_start(bare_install)

    assert res.exit_code == 1
    assert list(bare_install.rglob("*")) == [], sorted(
        str(p) for p in bare_install.rglob("*"))


# --------------------------------------------------------------------------- #
# structural: the gate is FIRST, and cannot drift back
# --------------------------------------------------------------------------- #

def test_the_dashboard_gate_runs_before_the_vault_is_opened():
    """Ordering is the fix, so ordering is what is pinned. A wording assertion
    would survive the reorder that reintroduces the defect."""
    source = inspect.getsource(cc.daemon_start.callback)
    gate = source.find("missing_groups")
    vault = source.find("_get_vault_and_config")

    assert gate != -1 and vault != -1, source
    assert gate < vault, (
        "`daemon start` opens the vault before it decides whether it will "
        "start at all -- a refusal on a bare install mints a vault tree in the "
        "operator's working directory")
