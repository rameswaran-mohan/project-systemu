"""D9 -- the remedy command is printed on its own line and is never wrapped.

WITNESSED DEFECT (v0.10.27, a stock install with no optional extras)
    ``systemu daemon start`` refuses, correctly, and prints the fix. On an
    80-column terminal the fix came out like this::

          UNAVAILABLE - Web dashboard is not installed (nicegui). Install it
        with: pip install "systemu[dashboard]"  Then: ...

    The whole refusal, remedy included, went through one Rich ``console.print``,
    and Rich wraps a paragraph to the terminal width wherever the break lands. A
    command split across a line break is not a command: it cannot be copied, and
    the half that survives a copy (``pip``) does nothing.

    This is the same class the ``roots`` group already avoids, and says so in as
    many words: "Output is line-oriented (click.echo, unwrapped) rather than a
    rich table on purpose: these lines carry absolute paths, and a path wrapped
    at column 80 is not a path."

THE FIX THIS PINS
    ``optional_deps`` mints the refusal in THREE pieces -- lead, command, tail --
    so the render layer can put the command on a line of its own while the
    sentence stays authored in one place; ``daemon start`` prints that command
    through ``click.echo``, which does not wrap.

    Pinned at 80 columns and at 40, because a remedy that survives only the width
    the author happened to test is not fixed.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from systemu.interface import cli_commands as cc
from systemu.runtime import optional_deps as od

#: The exact string an operator has to be able to copy in one piece.
_REMEDY = 'pip install "systemu[dashboard]"'


@pytest.fixture()
def only_the_dashboard_group_is_missing(monkeypatch):
    """The ONE probe, flipped for the dashboard group alone -- so the refusal
    under test is the witnessed one and not an unrelated group's."""
    dashboard = {od.canonical(p) for g in od.GROUPS if g.extra == "dashboard"
                 for p in g.packages}
    assert dashboard, "fixture premise: a 'dashboard' optional group exists"
    real = od.is_installed
    monkeypatch.setattr(od, "is_installed",
                        lambda pkg: False if od.canonical(pkg) in dashboard
                        else real(pkg))
    return dashboard


def _refusal(monkeypatch, width: int) -> str:
    """`daemon start`'s refusal, rendered at a fixed terminal width."""
    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setattr(cc, "console", cc.Console(width=width))
    monkeypatch.setattr(cc, "_get_vault_and_config", lambda ctx: (object(), object()))
    spawned = []
    monkeypatch.setattr("systemu.scheduler.daemon.start_daemon",
                        lambda **k: spawned.append(k))
    res = CliRunner().invoke(cc.daemon_start, [], obj={}, catch_exceptions=False)
    assert res.exit_code == 1, res.output
    assert spawned == [], "a daemon was spawned for a dashboard that cannot start"
    return res.output


# --------------------------------------------------------------------------- #
# THE PIN
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("width", [80, 40])
def test_the_remedy_command_survives_on_one_line(
        only_the_dashboard_group_is_missing, monkeypatch, width):
    """The witnessed failure, at the witnessed width and at half of it."""
    out = _refusal(monkeypatch, width)

    assert any(_REMEDY in ln for ln in out.splitlines()), (
        f"the remedy is not on any single line at width {width} -- it cannot be "
        f"copied:\n{out}")


@pytest.mark.parametrize("width", [80, 40])
def test_the_remedy_line_holds_the_command_and_nothing_else(
        only_the_dashboard_group_is_missing, monkeypatch, width):
    """Its OWN line. A command sharing a line with prose is one wording change
    away from wrapping again."""
    out = _refusal(monkeypatch, width)

    line = next(ln for ln in out.splitlines() if _REMEDY in ln)
    assert line.strip() == _REMEDY, (
        f"the remedy line carries more than the command: {line!r}")


def test_the_refusal_still_explains_itself(
        only_the_dashboard_group_is_missing, monkeypatch):
    """A fix that printed the bare command and dropped the sentence would pass
    both pins above and leave the operator with a command and no reason."""
    out = _refusal(monkeypatch, 80)

    assert "UNAVAILABLE" in out, out
    assert "nicegui" in out, out
    assert "dashboard is not installed" in out.lower(), out


def test_the_minted_sentence_and_its_parts_stay_the_same_wording(
        only_the_dashboard_group_is_missing):
    """The parts exist so ONE place authors the sentence. If they ever drift from
    the single-string form, two surfaces are telling operators different things."""
    lead, cmd, tail = od.unavailable_reason_parts(("nicegui",))
    assert cmd == _REMEDY, cmd
    assert (lead + cmd + tail) == od.unavailable_reason(("nicegui",))


def test_everything_installed_yields_no_parts():
    """No missing group, no refusal, no remedy -- in both shapes."""
    assert od.unavailable_reason_parts(()) == ("", "", "")
    assert od.unavailable_reason(()) == ""
