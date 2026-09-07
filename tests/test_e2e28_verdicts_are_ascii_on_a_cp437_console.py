"""N8 + N10 -- the verdict-carrying lines survive the console they are read on.

WITNESSED (v0.10.28, a scratch install, a Windows console on cp437/cp1252)

N8  Two verdict surfaces printed characters the console cannot encode:

      * the dashboard-missing refusal from ``daemon start`` -- a U+2717 BALLOT X
        in the headline and two U+2014 EM DASHes in the closing paragraph.  This
        is the FIRST thing a default install sees when it runs the advertised
        command, and it is a REFUSAL: the one message that must arrive intact.
      * ``table_payoff.format_inventory_hit`` -- a U+00B7 MIDDLE DOT separating
        the two halves of the payoff split, and a U+2014 EM DASH inside the
        CLAMP REGRESSION alarm, which is the loudest line the module can emit.
        The same alarm text is minted a second time by ``clamp_tripwire`` for
        the runtime warning path, and carried the same em dash.

N10 ``systemu info`` titled itself ``sharing_on - System Info``, with an em
    dash, on a program the operator installed and invoked as ``systemu``.  Two
    console scripts are one program (the root help says so), but the title of an
    inspection screen should name what was typed.

WHY THIS IS NOT COSMETIC (DEC-32c)
    A ``UnicodeEncodeError`` on a verdict is a verdict that did not arrive, and
    a mojibake one is a verdict the operator cannot quote, paste or search for.
    The bar is the one this repo already applies to its own subprocess captures:
    verdict-carrying output is ASCII.

SCOPE
    This file pins the SITES RULED, one assertion each, and does not sweep the
    product for decorative check marks -- a passing tick on a healthy result is
    not a verdict anybody has to read under duress.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from systemu.interface import cli_commands as cc
from systemu.runtime import optional_deps as od
from systemu.runtime import table_payoff


def _assert_ascii(text: str, what: str) -> None:
    bad = sorted({ch for ch in text if ord(ch) > 127})
    assert not bad, (
        f"{what} carries non-ASCII "
        f"{[hex(ord(c)) for c in bad]}; a cp437 console cannot print it:\n"
        + text.encode("ascii", "backslashreplace").decode())


# --------------------------------------------------------------------------- #
# N8 -- the dashboard-missing refusal
# --------------------------------------------------------------------------- #

def _refusal(monkeypatch, tmp_path) -> str:
    """Render the REAL refusal by making the REAL probe report nicegui absent.

    Nothing about the message is stubbed: ``missing_groups``,
    ``unavailable_reason_parts`` and ``install_command`` all run, so this
    witnesses the sentence an operator actually gets.  No daemon is spawned --
    the refusal returns before anything is started.
    """
    monkeypatch.setattr(od, "is_installed", lambda pkg: od.canonical(pkg) != "nicegui")
    vault_dir = tmp_path / "systemu" / "vault"
    vault_dir.mkdir(parents=True)
    res = CliRunner().invoke(
        cc.daemon_start, [],
        obj={"config": SimpleNamespace(vault_dir=str(vault_dir)),
             "vault": SimpleNamespace()},
        env={"COLUMNS": "80"},
    )
    assert res.exit_code == 1, (
        "the refusal must keep its nonzero exit -- not started is not success:\n"
        + res.output)
    return res.output


def test_the_dashboard_refusal_is_ascii(tmp_path, monkeypatch):
    _assert_ascii(_refusal(monkeypatch, tmp_path), "the dashboard-missing refusal")


def test_the_dashboard_refusal_still_says_what_is_wrong_and_how_to_fix_it(
        tmp_path, monkeypatch):
    """Anti-vacuity: ASCII is trivially achievable by saying nothing."""
    out = _refusal(monkeypatch, tmp_path)
    assert "dashboard is not installed" in out, out
    assert 'pip install "systemu[dashboard]"' in out, out


def test_the_refusals_install_command_is_still_on_one_unwrapped_line(
        tmp_path, monkeypatch):
    """F29 held while the characters changed: a wrapped command is not a
    command.  Pinned here because this commit rewrote the lines around it."""
    out = _refusal(monkeypatch, tmp_path)
    carriers = [ln for ln in out.splitlines()
                if 'pip install "systemu[dashboard]"' in ln]
    assert carriers, f"the remedy was folded by the console width:\n{out}"


# --------------------------------------------------------------------------- #
# N8 -- the inventory-hit lines
# --------------------------------------------------------------------------- #

#: A report with a payoff split AND a fired clamp, so every branch that emits a
#: character renders in one call.
_LOUD = {
    "supplied": 4, "avoided_gap": 3, "rate": 0.75,
    "silent": 1, "prefilled_confirm": 2,
    "table_supplied": 2, "table_avoided_gap": 2, "table_silent": 1,
}


def test_the_inventory_hit_lines_are_ascii():
    for line in table_payoff.format_inventory_hit(_LOUD):
        _assert_ascii(line, "format_inventory_hit")


def test_the_clamp_regression_alarm_is_ascii():
    """The loudest line the module emits, on the path that emits it."""
    lines = table_payoff.format_inventory_hit(_LOUD)
    alarm = [ln for ln in lines if "CLAMP REGRESSION" in ln]
    assert alarm, f"the fixture did not fire the alarm: {lines}"
    for line in alarm:
        _assert_ascii(line, "the CLAMP REGRESSION line")


#: A report shaped the way a snapshot round-trip delivers one (plain dicts),
#: carrying a table-sourced bind that the ask predicate calls SILENT -- i.e. the
#: state the clamp makes structurally impossible, which is what fires the wire.
_PUNCHED = {"per_objective": {"o": [
    {"source": "situation", "state": "have", "value_origin": "operator",
     "table_item_id": "t-1", "schema_path": "/a"},
]}}


def test_the_runtime_clamp_warning_is_ascii():
    """``clamp_tripwire`` mints the same alarm for the warning path, and carried
    the same em dash.  One alarm, two surfaces, one encoding rule."""
    out = table_payoff.clamp_tripwire(_PUNCHED)
    assert out["fired"] is True and out["message"], (
        f"the fixture did not fire the wire, so nothing was checked: {out}")
    _assert_ascii(out["message"], "the clamp_tripwire message")


def test_the_payoff_split_still_reports_both_halves():
    """Anti-vacuity: the separator changed, the two counts did not."""
    joined = "\n".join(table_payoff.format_inventory_hit(_LOUD))
    assert "bound with no ask: 1" in joined, joined
    assert "pre-filled one-click confirm: 2" in joined, joined


# --------------------------------------------------------------------------- #
# N10 -- the `info` title
# --------------------------------------------------------------------------- #

def _info_output() -> str:
    from sharing_on.cli import cli
    res = CliRunner().invoke(cli, ["info"], env={"COLUMNS": "100"})
    assert res.exit_code == 0, res.output
    return res.output


def test_info_titles_itself_with_the_name_the_operator_typed():
    assert "systemu - System Info" in _info_output(), _info_output()


def test_the_info_title_is_ascii():
    title = [ln for ln in _info_output().splitlines() if "System Info" in ln]
    assert title, _info_output()
    for line in title:
        # Rich draws the rule with box characters; the TITLE is the content.
        text = "".join(ch for ch in line if ch != "─")
        _assert_ascii(text, "the `info` title")
