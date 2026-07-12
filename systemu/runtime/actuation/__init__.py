"""R-A14a — the interface-blind actuation layer (MASTER-SPEC §8).

``ActuationModality`` is the one socket every actuation tier (http_api / mcp /
automation / cli / data_layer / uia / vision) implements, so the S1-S5 safety net
wraps every tier identically (§8.4). Slice 1 = the Protocol + dataclasses
(:mod:`.modality`); slice 2 = the ``mcp`` impl (:mod:`.mcp_modality`).
"""
from __future__ import annotations

from systemu.runtime.actuation.modality import (
    Action,
    ActionResult,
    ActuationModality,
)
from systemu.runtime.actuation.mcp_modality import McpActuationModality

__all__ = [
    "Action",
    "ActionResult",
    "ActuationModality",
    "McpActuationModality",
]
