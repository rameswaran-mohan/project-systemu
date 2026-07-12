"""R-A14a slice 1 — the ActuationModality Protocol contract (MASTER-SPEC §8.2).

One interface-blind actuation socket. This slice defines the Protocol + the small
Action / ActionResult dataclasses. The tests assert the SHAPE: a conforming impl
satisfies the runtime-checkable Protocol; a non-conforming one does not.
"""
from __future__ import annotations

from typing import Any, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
#  a conforming stub — satisfies every §8.2 member
# ─────────────────────────────────────────────────────────────────────────────

class _ConformingModality:
    name = "stub"
    reliability_tier = 9

    def probe(self, target: Any = None) -> bool:
        return True

    def discover_affordances(self, target: Any = None) -> List[Any]:
        return []

    def propose_action(self, objective: Any, *args: Any, **kwargs: Any):
        from systemu.runtime.actuation.modality import Action
        return Action(modality=self.name)

    def execute(self, action: Any, *, gate: Any = None):
        from systemu.runtime.actuation.modality import ActionResult
        return ActionResult(success=True)

    def capture_evidence(self, action: Any, result: Any) -> Optional[Any]:
        return None


class _MissingCaptureEvidence:
    """Non-conforming: omits capture_evidence (a modality that can't produce the
    §5.8 independent confirmation is not admissible)."""
    name = "bad"
    reliability_tier = 1

    def probe(self, target=None):
        return True

    def discover_affordances(self, target=None):
        return []

    def propose_action(self, objective, *a, **k):
        return None

    def execute(self, action, *, gate=None):
        return None
    # NO capture_evidence


def test_conforming_impl_satisfies_protocol():
    from systemu.runtime.actuation.modality import ActuationModality
    assert isinstance(_ConformingModality(), ActuationModality), (
        "a stub with every §8.2 member must satisfy the runtime-checkable Protocol")


def test_nonconforming_impl_does_not_satisfy_protocol():
    from systemu.runtime.actuation.modality import ActuationModality
    assert not isinstance(_MissingCaptureEvidence(), ActuationModality), (
        "an impl missing capture_evidence must NOT satisfy the Protocol")
    # a plain object clearly fails too
    assert not isinstance(object(), ActuationModality)


def test_action_and_result_dataclasses_roundtrip():
    from systemu.runtime.actuation.modality import Action, ActionResult
    a = Action(modality="mcp", target="https://srv", name="create_issue",
               params={"title": "x"}, is_mutation=True)
    assert a.modality == "mcp" and a.is_mutation is True
    assert a.params == {"title": "x"}
    # defaults
    b = Action(modality="mcp")
    assert b.params == {} and b.is_mutation is False and b.objective is None

    r = ActionResult(success=True, response={"ok": 1})
    assert r.success is True and r.response == {"ok": 1} and r.error == ""


def test_mcp_modality_is_a_conforming_modality():
    """The real `mcp` impl (slice 2) satisfies the Protocol too."""
    from systemu.runtime.actuation.modality import ActuationModality
    from systemu.runtime.actuation.mcp_modality import McpActuationModality
    m = McpActuationModality(runtime=None)
    assert isinstance(m, ActuationModality)
    assert m.name == "mcp" and m.reliability_tier == 2


def test_package_reexports():
    """The package surface re-exports the contract + the mcp impl."""
    import systemu.runtime.actuation as act
    assert hasattr(act, "ActuationModality")
    assert hasattr(act, "Action") and hasattr(act, "ActionResult")
    assert hasattr(act, "McpActuationModality")
