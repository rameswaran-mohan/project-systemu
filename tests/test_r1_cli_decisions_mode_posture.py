"""R1 -- ``systemu decisions mode --set <mode>`` (the gate-mode dial's console twin)
LOWERS approval requirements on a loosening, so it takes the same typed phrase as
the Settings card, passed as ``--confirm "<phrase>"``; tightening stays one command.

THE WITNESSED GAP: ``decisions mode --set bypass`` persisted Bypass on one command.

``--set`` writes the mode and RESETS overrides and the floor (``save_gate_mode_
settings(mode=...)``), so the proposal compared is exactly that: dropping a "deny"
override is a loosening the phrase must name too. Writes go to a tmp cwd .env.
"""
from __future__ import annotations

import os

import pytest
from click.testing import CliRunner


@pytest.fixture
def dial(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SYSTEMU_GATE_MODE", "risk_tiered")
    monkeypatch.setenv("SYSTEMU_GATE_OVERRIDES", "{}")
    monkeypatch.setenv("SYSTEMU_GATE_NO_FLOOR", "0")
    return tmp_path / ".env"


def _mode(*args):
    from systemu.interface.cli_commands import decisions_group
    return CliRunner().invoke(decisions_group, ["mode", *args])


def _env(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_set_bypass_without_confirm_is_refused_and_names_the_phrase(dial):
    res = _mode("--set", "bypass")
    assert res.exit_code == 2
    assert '--confirm "bypass"' in res.output
    assert "Nothing was saved" in res.output
    assert os.environ["SYSTEMU_GATE_MODE"] == "risk_tiered"
    assert _env(dial) == ""


@pytest.mark.parametrize("phrase", ["", "BYPASS", "by pass", "risk_tiered"])
def test_a_wrong_confirm_is_refused(dial, phrase):
    res = _mode("--set", "bypass", "--confirm", phrase)
    assert res.exit_code == 2
    assert os.environ["SYSTEMU_GATE_MODE"] == "risk_tiered"


def test_set_bypass_with_the_phrase_saves_and_still_warns(dial):
    from systemu.interface.command.gate_mode import GateMode, load_default_policy
    res = _mode("--set", "bypass", "--confirm", "bypass")
    assert res.exit_code == 0, res.output
    assert load_default_policy().mode is GateMode.BYPASS
    assert "SYSTEMU_GATE_MODE=bypass" in _env(dial)
    assert "DANGER" in res.output.upper()


def test_tightening_stays_one_command(dial, monkeypatch):
    monkeypatch.setenv("SYSTEMU_GATE_MODE", "bypass")
    monkeypatch.setenv("SYSTEMU_GATE_NO_FLOOR", "1")
    res = _mode("--set", "approve_only")
    assert res.exit_code == 0, res.output
    assert os.environ["SYSTEMU_GATE_MODE"] == "approve_only"


def test_dropping_a_deny_override_is_named_in_the_phrase(dial, monkeypatch):
    """``--set`` resets overrides: a "deny" pin that disappears hands that gate type
    to the mode default, which can now run it -- a loosening, named."""
    monkeypatch.setenv("SYSTEMU_GATE_OVERRIDES", '{"forge": "deny"}')
    res = _mode("--set", "risk_tiered")
    assert res.exit_code == 2
    assert '--confirm "forge=default"' in res.output
    res = _mode("--set", "risk_tiered", "--confirm", "forge=default")
    assert res.exit_code == 0, res.output


def test_showing_the_dial_needs_no_confirm(dial):
    res = _mode()
    assert res.exit_code == 0, res.output
    assert "risk_tiered" in res.output
