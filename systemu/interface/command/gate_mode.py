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


FLOOR_GATE_TYPES = frozenset({"dep", "recovery"})
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
