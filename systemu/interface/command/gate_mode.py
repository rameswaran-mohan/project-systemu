"""Gate mode dial (spec §4.3 / D4) + the Bypass floor (D5).

Generalizes the shipped adherence dial (settings.adherence_card /
runtime.adherence). Risk-tiered IS the reverse-harness Governor: it consumes
the RiskBand the Governor already assigned (low/medium/high) — it does NOT
re-derive risk.
"""
from __future__ import annotations
from enum import Enum
from typing import Dict, Optional
from pydantic import BaseModel, Field


class GateMode(str, Enum):
    BYPASS       = "bypass"        # auto-grant except the floor (D5)
    RISK_TIERED  = "risk_tiered"   # the Governor (default)
    APPROVE_ONLY = "approve_only"  # always ask


FLOOR_GATE_TYPES = frozenset({"dep", "recovery", "command", "mcp", "mcp_call", "sampling", "tool",
                              # IMPL-4: the one-time bulk first-gate review card. One
                              # resolution can bless an ENTIRE tool inventory, so it is
                              # the last card that may ever be auto-granted by a Bypass
                              # policy (which has born-resolved a gate with zero clicks
                              # in this codebase before).
                              "tool_bulk"})
FLOOR_CAPABILITIES = frozenset({
    "network-egress", "fs-write", "pkg-install",
    "destructive-recovery", "credential-access",
})


class GateModePolicy(BaseModel):
    mode:      GateMode = GateMode.RISK_TIERED
    overrides: Dict[str, str] = Field(default_factory=dict)
    no_floor:  bool = False
    floor_extra: frozenset = Field(default_factory=frozenset)
    model_config = {"extra": "forbid"}

    def _on_floor(self, gate_type: str, capability: str) -> bool:
        if self.no_floor:
            return False
        floor = FLOOR_GATE_TYPES | self.floor_extra
        return gate_type in floor or capability in FLOOR_CAPABILITIES

    def decide(self, *, risk: str, gate_type: str, capability: str = "") -> str:
        """Return one of 'allow' | 'ask' | 'deny'."""
        if gate_type in self.overrides:
            return self.overrides[gate_type]
        if self._on_floor(gate_type, capability):
            return "ask"
        if self.mode is GateMode.APPROVE_ONLY:
            return "ask"
        if self.mode is GateMode.BYPASS:
            return "allow"
        return "allow" if str(risk).lower() == "low" else "ask"


def floor_pierces(policy: "GateModePolicy") -> list[str]:
    """Human-readable list of the ways ``policy`` bypasses the safety floor
    (W2.4 — pure, renderable as a warn banner).

    The escape hatches are DELIBERATE (override-beats-floor is the operator's
    documented out), but they must be visible: ``no_floor`` disables the floor
    wholesale, and an ``allow`` override on a floor gate type auto-grants what
    the floor exists to force into review.  ``ask`` overrides match floor
    behaviour and non-floor overrides are the dial working as designed —
    neither is flagged.
    """
    out: list[str] = []
    if policy.no_floor:
        out.append("no_floor=true — the safety floor is disabled entirely")
    floor = FLOOR_GATE_TYPES | policy.floor_extra
    for gate_type, value in sorted(policy.overrides.items()):
        if gate_type in floor and value == "allow":
            out.append(
                f"override {gate_type}→allow auto-grants a floor gate type "
                f"(pierces the safety floor)"
            )
    return out


def load_default_policy() -> "GateModePolicy":
    """Build a GateModePolicy from the persisted gate-mode settings (.env).

    Maps the stored mode string → GateMode and carries overrides + no_floor.
    Import-light: gate_mode_settings reads only env, no NiceGUI dependency.
    Falls back to the default Risk-tiered policy if settings are unreadable so
    a missing/garbled .env never disables gating entirely.
    """
    try:
        from systemu.runtime.gate_mode_settings import get_gate_mode_settings
        state = get_gate_mode_settings()
        return GateModePolicy(
            mode=GateMode(state.get("mode", "risk_tiered")),
            overrides=dict(state.get("overrides") or {}),
            no_floor=bool(state.get("no_floor", False)),
        )
    except Exception:
        return GateModePolicy()
