"""Import-light tests for the Settings gate_mode_card pure model (Task 15).

Mirrors the adherence-card test style: assert the pure helper that decides
"show the Bypass danger banner" and builds the mode option list, WITHOUT
standing up a NiceGUI runtime.
"""
from __future__ import annotations

from systemu.interface.command.gate_mode import GateMode


def _model(**settings):
    from systemu.interface.pages.settings import _gate_mode_card_model
    return _gate_mode_card_model(settings)


# ── danger banner (D4 / P12 — never silent about Bypass) ──────────────────────

def test_danger_banner_shown_for_bypass():
    assert _model(mode="bypass")["show_danger_banner"] is True


def test_danger_banner_hidden_for_risk_tiered():
    assert _model(mode="risk_tiered")["show_danger_banner"] is False


def test_danger_banner_hidden_for_approve_only():
    assert _model(mode="approve_only")["show_danger_banner"] is False


def test_danger_banner_hidden_for_unknown_mode_default():
    # A missing/garbled mode must NOT light the danger banner.
    assert _model()["show_danger_banner"] is False


# ── mode options = the three GateMode values ──────────────────────────────────

def test_mode_options_are_the_three_gate_modes():
    options = _model(mode="risk_tiered")["mode_options"]
    assert set(options.keys()) == {m.value for m in GateMode}
    assert set(options.keys()) == {"bypass", "risk_tiered", "approve_only"}


def test_mode_options_have_human_labels():
    options = _model(mode="risk_tiered")["mode_options"]
    # Each value maps to a non-empty, human-readable label.
    assert all(isinstance(v, str) and v.strip() for v in options.values())


def test_model_carries_selected_mode():
    assert _model(mode="approve_only")["mode"] == "approve_only"
