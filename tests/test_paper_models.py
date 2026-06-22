"""Paper-readiness model-field additions (Plan 0 Builds 1 & 2).

Pull-decision instrumentation + structured ledger + tool provenance.
"""
import pytest
from pydantic import ValidationError

from systemu.core.models import (
    HarnessRequest, HarnessVerdict, HarnessKind, HarnessDecision, Tool, ToolType,
)


# ── Build 2: HarnessVerdict.decided_by ───────────────────────────────────────
def test_verdict_decided_by_defaults_deterministic():
    v = HarnessVerdict(decision=HarnessDecision.GRANT)
    assert v.decided_by == "deterministic"


def test_verdict_decided_by_accepts_llm():
    v = HarnessVerdict(decision=HarnessDecision.DENY, decided_by="llm")
    assert v.decided_by == "llm"


def test_verdict_decided_by_rejects_unknown():
    with pytest.raises(ValidationError):
        HarnessVerdict(decision=HarnessDecision.GRANT, decided_by="operator")


# ── Build 1: HarnessVerdict.request_outcome ──────────────────────────────────
def test_verdict_request_outcome_defaults_none():
    v = HarnessVerdict(decision=HarnessDecision.GRANT)
    assert v.request_outcome is None


def test_verdict_request_outcome_accepts_literals():
    for val in ("granted_used", "granted_unused", "denied_fallback_ok",
                "denied_fallback_failed", "escalate_unresolved"):
        v = HarnessVerdict(decision=HarnessDecision.GRANT, request_outcome=val)
        assert v.request_outcome == val


def test_verdict_request_outcome_rejects_unknown():
    with pytest.raises(ValidationError):
        HarnessVerdict(decision=HarnessDecision.GRANT, request_outcome="banana")


# ── Build 1: HarnessRequest instrumentation fields ───────────────────────────
def test_request_pulldecision_defaults():
    r = HarnessRequest(kind=HarnessKind.TOOL)
    assert r.confidence == 0.5
    assert r.attempts_before_request == 0
    assert r.provenance == {}


def test_request_confidence_bounds():
    HarnessRequest(kind=HarnessKind.TOOL, confidence=0.0)
    HarnessRequest(kind=HarnessKind.TOOL, confidence=1.0)
    with pytest.raises(ValidationError):
        HarnessRequest(kind=HarnessKind.TOOL, confidence=1.5)
    with pytest.raises(ValidationError):
        HarnessRequest(kind=HarnessKind.TOOL, confidence=-0.1)


def test_request_provenance_roundtrip():
    prov = {"tool_attempts": [{"name": "x", "failures": 2}], "blocked_signals": ["loop_guard"]}
    r = HarnessRequest(kind=HarnessKind.TOOL, attempts_before_request=3, provenance=prov)
    assert r.attempts_before_request == 3
    assert r.provenance["blocked_signals"] == ["loop_guard"]
    # survives JSON round-trip
    r2 = HarnessRequest.model_validate_json(r.model_dump_json())
    assert r2.provenance == prov


# ── Build 2: Tool.forged_by_execution_id ─────────────────────────────────────
def test_tool_forged_by_execution_id_defaults_none():
    t = Tool(id="t1", name="t1", description="d", tool_type=ToolType.PYTHON_FUNCTION)
    assert t.forged_by_execution_id is None


def test_tool_forged_by_execution_id_settable():
    t = Tool(id="t1", name="t1", description="d", tool_type=ToolType.PYTHON_FUNCTION,
             forged_by_execution_id="exec_abc")
    assert t.forged_by_execution_id == "exec_abc"
