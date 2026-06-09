"""Tests for .env-backed gate-mode settings (mirrors adherence settings)."""
import os

import pytest


def test_gate_mode_settings_round_trip(tmp_path, monkeypatch):
    """save → get round-trips mode + overrides + no_floor through the .env
    writer and the live os.environ patch (never touches the repo .env)."""
    monkeypatch.chdir(tmp_path)          # .env writer targets cwd/.env
    # Start from a clean environment for the gate vars.
    for k in ("SYSTEMU_GATE_MODE", "SYSTEMU_GATE_OVERRIDES", "SYSTEMU_GATE_NO_FLOOR"):
        monkeypatch.delenv(k, raising=False)

    from systemu.runtime.gate_mode_settings import (
        save_gate_mode_settings, get_gate_mode_settings)

    save_gate_mode_settings(mode="bypass", overrides={"forge": "deny"}, no_floor=True)

    # Persisted to .env in the tmp cwd.
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "SYSTEMU_GATE_MODE=bypass" in env_text
    assert "SYSTEMU_GATE_NO_FLOOR=" in env_text
    assert "forge" in env_text  # overrides json

    # Live os.environ patched immediately (no reload needed).
    assert os.environ["SYSTEMU_GATE_MODE"] == "bypass"

    state = get_gate_mode_settings()
    assert state["mode"] == "bypass"
    assert state["overrides"] == {"forge": "deny"}
    assert state["no_floor"] is True


def test_gate_mode_settings_invalid_mode_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from systemu.runtime.gate_mode_settings import save_gate_mode_settings
    with pytest.raises(ValueError):
        save_gate_mode_settings(mode="banana")
