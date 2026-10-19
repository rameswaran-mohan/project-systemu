"""R1 -- ``systemu settings set gate_mode <mode>`` is a CLI path that LOWERS approval
requirements, so it takes the same typed phrase as the Settings card, passed as
``--confirm "<phrase>"``; tightening stays one command.

THE WITNESSED GAP: ``settings set gate_mode bypass`` wrote SYSTEMU_GATE_MODE=bypass
through ``verbs.settings_set`` on one command, with no confirmation at all.

The persistence seam (``verbs._persist_setting``) is recorded instead of writing the
operating home's .env; the CURRENT posture is the real env, set per test.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from systemu.interface import cli_commands
from systemu.interface.command import verbs


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setenv("SYSTEMU_GATE_MODE", "risk_tiered")
    monkeypatch.setenv("SYSTEMU_GATE_OVERRIDES", "{}")
    monkeypatch.setenv("SYSTEMU_GATE_NO_FLOOR", "0")
    writes = []
    monkeypatch.setattr(verbs, "_persist_setting",
                        lambda key, value: writes.append((key, value)))
    monkeypatch.setattr(cli_commands, "_get_vault_and_config",
                        lambda ctx: (object(), object()))
    return writes


def _invoke(*args):
    return CliRunner().invoke(cli_commands.settings_cmd, ["set", *args])


def test_setting_gate_mode_to_bypass_without_confirm_is_refused(cli):
    res = _invoke("gate_mode", "bypass")
    assert res.exit_code != 0
    assert cli == []                                   # nothing persisted
    assert "--confirm" in res.output and "bypass" in res.output
    assert "Nothing was saved" in res.output


@pytest.mark.parametrize("phrase", ["", "Bypass", "yes", "risk_tiered"])
def test_a_wrong_confirm_is_refused(cli, phrase):
    res = _invoke("gate_mode", "bypass", "--confirm", phrase)
    assert res.exit_code != 0
    assert cli == []


def test_the_right_confirm_saves(cli):
    res = _invoke("gate_mode", "bypass", "--confirm", "bypass")
    assert res.exit_code == 0, res.output
    assert cli == [("gate_mode", "bypass")]


def test_tightening_gate_mode_stays_one_command(cli):
    res = _invoke("gate_mode", "approve_only")
    assert res.exit_code == 0, res.output
    assert cli == [("gate_mode", "approve_only")]


def test_a_non_posture_setting_needs_no_confirm(cli):
    res = _invoke("non_interactive", "true")
    assert res.exit_code == 0, res.output
    assert cli == [("non_interactive", "true")]
