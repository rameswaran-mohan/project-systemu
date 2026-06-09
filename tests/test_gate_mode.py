"""Tests for the gate-mode dial (spec §4.3 / D4) + the Bypass floor (D5)."""
from systemu.interface.command.gate_mode import GateMode, GateModePolicy


def test_approve_only_always_asks():
    p = GateModePolicy(mode=GateMode.APPROVE_ONLY)
    # Even a low-risk, non-floor gate must ask under Approve-only.
    assert p.decide(risk="low", gate_type="scroll") == "ask"
    assert p.decide(risk="high", gate_type="forge") == "ask"


def test_risk_tiered_grants_low_asks_high():
    p = GateModePolicy(mode=GateMode.RISK_TIERED)
    # Risk-tiered IS the Governor: it consumes the assigned RiskBand.
    assert p.decide(risk="low", gate_type="scroll") == "allow"
    assert p.decide(risk="high", gate_type="forge") == "ask"
    assert p.decide(risk="medium", gate_type="forge") == "ask"


def test_bypass_auto_grants_except_floor():
    p = GateModePolicy(mode=GateMode.BYPASS)
    # High-risk but NON-floor gate → auto-grant under Bypass.
    assert p.decide(risk="high", gate_type="forge") == "allow"
    # Floor gate types still ask, even under Bypass (D5).
    assert p.decide(risk="low", gate_type="dep") == "ask"
    assert p.decide(risk="low", gate_type="recovery") == "ask"
    # A floor capability also forces ask, regardless of gate_type.
    assert p.decide(risk="low", gate_type="forge",
                    capability="credential-access") == "ask"


def test_per_type_override_wins():
    # An explicit per-type override beats mode AND the floor.
    p = GateModePolicy(mode=GateMode.BYPASS, overrides={"forge": "deny"})
    assert p.decide(risk="high", gate_type="forge") == "deny"
    # Override also wins for a floor type.
    p2 = GateModePolicy(mode=GateMode.RISK_TIERED, overrides={"dep": "allow"})
    assert p2.decide(risk="high", gate_type="dep") == "allow"


def test_no_floor_bypass_grants_everything():
    p = GateModePolicy(mode=GateMode.BYPASS, no_floor=True)
    # With no_floor, even a floor gate type is auto-granted under Bypass.
    assert p.decide(risk="high", gate_type="dep") == "allow"
    assert p.decide(risk="high", gate_type="recovery") == "allow"
    assert p.decide(risk="low", gate_type="forge",
                    capability="credential-access") == "allow"
