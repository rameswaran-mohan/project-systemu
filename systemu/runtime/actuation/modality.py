"""R-A14a slice 1 — the ``ActuationModality`` contract (MASTER-SPEC §8.2).

One **interface-blind actuation socket**: every actuation modality (``http_api`` /
``mcp`` / ``automation`` / ``cli`` / ``data_layer`` / ``uia`` / ``vision``, §8.3)
implements the SAME Protocol, so the safety net — S1 gate, S3 independent verify, S4
fail-closed credit — wraps every tier IDENTICALLY (§8.4: "written once, applied to
all seven tiers"). This slice defines the Protocol + the small ``Action`` /
``ActionResult`` dataclasses; slice 2 adds the ``mcp`` impl.

The contract (SPEC:733-744) — a modality that cannot produce an inspectable,
gateable action is **not admissible** (SPEC:744)::

    class ActuationModality(Protocol):
        name: str            # "http_api"|"mcp"|"automation"|"cli"|"data_layer"|"uia"|"vision"
        reliability_tier: int
        def probe(target) -> Availability
        def discover_affordances(target) -> list[Affordance]
        def propose_action(objective, affordance) -> ProposedAction   # INSPECTABLE
        def execute(action, *, gate: ActionGate) -> ActionResult      # THROUGH the S1 gate; never bypasses
        def capture_evidence(action, result) -> ExternalEvidence      # the independent (§5.8) confirmation

``execute()`` routing every effect THROUGH S1 is the load-bearing invariant §9 rests
on: "no actuation tier is admissible until S1–S5 exist AND ``ActuationModality.
execute()`` provably routes every tier through them." The R-A14a milestone builds the
Protocol + the ``mcp`` impl (whose ``execute`` delegates to the existing gated MCP
chokepoint) so a future CI "every execute() through S1" assertion is meaningful.

Kept a PURE Protocol + dataclasses (no runtime imports beyond the ExternalEvidence
model) so the contract stays cycle-free and importable from any layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from systemu.core.models import ExternalEvidence


@dataclass
class Action:
    """An INSPECTABLE, gateable actuation the modality proposes / executes.

    Interface-blind: the SAME shape whether the effect is an HTTP POST, an MCP tool
    call, or a desktop-automation step. ``is_mutation`` marks a KNOWN-mutation (not a
    read) — the signal the S3/S4 linkage keys the per-actuation verification
    obligation off (SPEC §8.2 ``capture_evidence`` fires for a mutation, never a
    read). ``objective`` carries the completing Objective so the money-move
    classification (BLOCKER-3) can run over its goal/params."""

    modality: str                                       # "mcp" | "http_api" | ...
    target: str = ""                                    # server / host / system id
    name: str = ""                                      # affordance / tool name
    params: Dict[str, Any] = field(default_factory=dict)
    is_mutation: bool = False                           # known-mutation (not a read)
    objective: Any = None                               # the completing Objective (money-move class)
    tool: Any = None                                    # the resolved tool descriptor (effect_tags/meta)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    """The outcome of :meth:`ActuationModality.execute` — the guarded effect result.

    ``response`` is the modality's structured result (for MCP: the guarded transport
    envelope, from which ``capture_evidence`` derives the readback_url / expected
    tokens). ``raw`` keeps the untouched transport envelope for audit."""

    success: bool
    response: Any = None
    error: str = ""
    raw: Any = None


@runtime_checkable
class ActuationModality(Protocol):
    """The one interface-blind actuation socket (SPEC §8.2).

    ``@runtime_checkable`` so a CI/selector can assert an impl is admissible by
    STRUCTURE (every §8.2 member present) — checking presence, not signatures. The
    signatures below are the simplified R-A14a shapes (``probe() -> bool`` etc.); the
    ``target``/``gate`` params are optional so a modality can stay faithful to the
    full §8.2 signatures without breaking the structural check."""

    name: str
    reliability_tier: int

    def probe(self, target: Any = None) -> bool: ...
    def discover_affordances(self, target: Any = None) -> List[Any]: ...
    def propose_action(self, objective: Any, *args: Any, **kwargs: Any) -> Action: ...
    def execute(self, action: Action, *, gate: Any = None) -> ActionResult: ...
    def capture_evidence(
        self, action: Action, result: ActionResult
    ) -> Optional[ExternalEvidence]: ...
