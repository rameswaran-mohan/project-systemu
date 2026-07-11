"""S4 wave1 Step 0 — trigger-stamp None-capability guard (the headline gap).

`requirement_binder._requires_external_verification(None)` USED to return True
(because `_get(None,"effect_tags")` → None → [] → the "empty/unknown ⇒ True"
branch). The ONLY live binder call pre-loop passes ``capability=None``
(shadow_runtime's producer), so pre-guard it would stamp EVERY objective
``requires_external_verification=True`` — a real external-effect trigger that
never reflected a REAL EffectTag classification.

The guard: a None/absent capability must NOT stamp external. The real
per-objective capability (and thus the real EffectTag classification) lands with
R-A12; until then the trigger stays False for a None capability.

These tests DIRECTLY exercise ``compute_requirements`` (the model/guard), NOT the
full execute() loop. The AC4 truths (a real UNKNOWN/empty-tag capability → True,
net_mutate → True, local_read → False) are re-asserted here so the guard does not
over-correct — mirroring the setups in tests/test_ra10_binder.py:241-281 WITHOUT
editing that (contract) file.
"""
from __future__ import annotations

from systemu.core.models import Objective, Tool
from systemu.runtime.requirement_binder import (
    compute_requirements,
    _requires_external_verification,
)


# ── tiny fakes (mirror test_ra10_binder.py's, kept local so the contract file
#    is not edited) ────────────────────────────────────────────────────────────
class _FakeGrantedRoots:
    def __init__(self, roots):
        self._roots = list(roots or [])

    def is_within_granted(self, candidate: str) -> bool:
        return False


class _FakeCtx:
    def __init__(self, *, situation=None, granted_roots=None):
        self._situation_report = situation
        self._granted_roots = granted_roots
        self.files_produced = []
        self.vault = None


def _tool(name, schema, *, effect_tags=None, channel=None):
    return Tool(
        id="tool_" + name,
        name=name,
        description="test tool",
        tool_type="python_function",
        parameters_schema=schema,
        effect_tags=list(effect_tags or []),
        external_verification_channel=channel,
    )


def _obj(oid=1):
    return Objective(id=oid, goal="do the thing", success_criteria="it is done")


def _situation(**over):
    base = {
        "services": [], "capabilities": [], "roots": [],
        "credentials": [], "profile": {}, "declared_intents": [],
    }
    base.update(over)
    return base


# ── Step 0: the guard — a None capability must NOT stamp external ─────────────
def test_none_capability_does_not_stamp_external():
    """The live pre-loop producer passes capability=None. That must leave the
    objective's trigger False (no REAL EffectTag classification exists yet)."""
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    obj = _obj()
    # objectives default requires_external_verification=False; assert we don't flip it
    assert obj.requires_external_verification is False
    compute_requirements(obj, None, situation, ctx)
    assert obj.requires_external_verification is False


def test_none_capability_helper_returns_false():
    """The unit-level guard: the classifier itself refuses to call a None
    capability dangerous — the trigger must reflect a REAL classification."""
    assert _requires_external_verification(None) is False


# ── AC4 re-assertions, DEC-24 + S4_STAMP (supersedes INT-1): the DEC-24 classifier
# restores the unconditional dangerous ⇒ stamp VALUE (net_mutate/unknown/empty ⇒ True;
# local_read ⇒ False — no channel gate). Whether that value is WRITTEN is governed by
# S4_STAMP (off/shadow/enforce); the WRITE is asserted under enforce (OFF, the default,
# never writes it — Stage 1). R-A13 Stage 3 removes the flag and writes live.
def test_ac4_unknown_tag_still_stamps(monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("mystery", {"x": {"type": "string"}}, effect_tags=["unknown"])
    obj = _obj()
    compute_requirements(obj, cap, situation, ctx)
    assert obj.requires_external_verification is True


def test_ac4_empty_tags_still_stamps(monkeypatch):
    """An EMPTY effect_tags list is UNKNOWN-until-classified ⇒ DEC-24 True (BLOCKER-3)."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("blank", {"x": {"type": "string"}}, effect_tags=[])
    obj = _obj()
    compute_requirements(obj, cap, situation, ctx)
    assert obj.requires_external_verification is True


def test_ac4_net_mutate_still_stamps(monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("poster", {"x": {"type": "string"}}, effect_tags=["net_mutate"])
    obj = _obj()
    compute_requirements(obj, cap, situation, ctx)
    assert obj.requires_external_verification is True


def test_ac4_local_read_still_does_not_stamp(monkeypatch):
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    situation = _situation()
    ctx = _FakeCtx(situation=situation, granted_roots=_FakeGrantedRoots([]))
    cap = _tool("reader", {"x": {"type": "string"}}, effect_tags=["local_read"])
    obj = _obj()
    compute_requirements(obj, cap, situation, ctx)
    assert obj.requires_external_verification is False
