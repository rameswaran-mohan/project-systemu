"""R-A14a — the interface-blind actuation layer (MASTER-SPEC §8).

``ActuationModality`` is the one socket every actuation tier (http_api / mcp /
automation / cli / data_layer / uia / vision) implements, so the S1-S5 safety net
wraps every tier identically (§8.4). Slice 1 = the Protocol + dataclasses
(:mod:`.modality`); slice 2 = the ``mcp`` impl (:mod:`.mcp_modality`).
"""
from __future__ import annotations

from typing import Any, List

from systemu.runtime.actuation.modality import (
    Action,
    ActionResult,
    ActuationModality,
)
from systemu.runtime.actuation.mcp_modality import McpActuationModality

# ── R-A14a §15.1(b) / DEC-1 — the ActuationModality SELECTOR (MASTER-SPEC §8.3) ─
# Pre-S2 there is NO OS-kernel egress jail, so ONLY the tier-2 `mcp`
# (operator-connected, in-daemon, token-parent-side) rung is admissible. A
# forged-tool actuation rung (tier-1) and a registry-install rung are NOT offered
# until S2 ships (IMPL-13): admitting one would let an approved forged network
# actuator run with unrestricted egress. This selector is the single source of
# truth the §15.1(b) CI gate asserts against — it must NEVER grow a
# forged/registry rung until the enforcer exists.
#
# The §8.3 tiers deliberately WITHHELD until S2 (documented, not selectable):
_S2_GATED_MODALITY_NAMES = frozenset({
    "forged", "registry", "http_api", "cli", "automation", "data_layer",
    "uia", "vision",
})


def admissible_modality_names() -> "frozenset[str]":
    """The actuation rungs admissible in the CURRENT build. Pre-S2 that is
    EXACTLY ``{"mcp"}`` — no forged/registry rung (§15.1(b))."""
    return frozenset({McpActuationModality.name})


def admissible_modalities(runtime: Any = None, *, vault: Any = None,
                          config: Any = None) -> "List[ActuationModality]":
    """Instantiate the admissible actuation modalities for the current build.

    Pre-S2 this is exactly the `mcp` rung. The forged-tool and registry-install
    rungs are constructed here ONLY once S2's egress jail is the sole spawn path
    (R-A14b / R-A8 Phase-2). Keeping the list here — not scattered at call sites —
    makes the §15.1(b) "no forged/registry rung" gate a single, enforceable truth."""
    return [McpActuationModality(runtime, vault=vault, config=config)]


__all__ = [
    "Action",
    "ActionResult",
    "ActuationModality",
    "McpActuationModality",
    "admissible_modality_names",
    "admissible_modalities",
]
