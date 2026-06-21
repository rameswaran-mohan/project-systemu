"""v0.9.7 Phase 1.1 — HarnessRequest / HarnessVerdict models (Reverse-Harness)."""
import pytest
from pydantic import ValidationError

from systemu.core.models import (
    HarnessKind, HarnessRequest, HarnessVerdict, HarnessDecision, RiskBand,
)


def test_harness_request_defaults():
    r = HarnessRequest(kind=HarnessKind.TOOL, rationale="need an ip-geo tool")
    assert r.kind == HarnessKind.TOOL
    assert r.blocking is True
    assert r.urgency == "normal"
    assert r.spec == {}
    assert r.request_id.startswith("hreq_") and len(r.request_id) > 5


def test_harness_request_requires_kind():
    with pytest.raises(ValidationError):
        HarnessRequest(rationale="no kind")


def test_harness_request_forbids_extra():
    with pytest.raises(ValidationError):
        HarnessRequest(kind=HarnessKind.SKILL, bogus_field=1)


def test_harness_request_kinds_cover_all_families():
    assert {k.value for k in HarnessKind} == {
        "tool", "skill", "access", "compute", "subagent", "input", "mcp",
    }


def test_harness_request_json_round_trip():
    r = HarnessRequest(
        kind=HarnessKind.TOOL,
        spec={"name": "ip_geolocate", "parameters_schema": {"ip": {"type": "string"}}},
        rationale="resolve city from IP", fallback="ask the operator", urgency="high",
        blocking=False,
    )
    rebuilt = HarnessRequest.model_validate_json(r.model_dump_json())
    assert rebuilt.kind == HarnessKind.TOOL
    assert rebuilt.spec["name"] == "ip_geolocate"
    assert rebuilt.blocking is False
    assert rebuilt.urgency == "high"


def test_harness_verdict_construction_and_defaults():
    v = HarnessVerdict(decision=HarnessDecision.GRANT, request_id="hreq_abc")
    assert v.decision == HarnessDecision.GRANT
    assert v.risk_band == RiskBand.LOW
    assert v.alternatives == []
    assert v.lease_id is None


def test_harness_verdict_escalate_with_alternatives():
    v = HarnessVerdict(
        decision=HarnessDecision.DENY, risk_band=RiskBand.HIGH,
        rationale="network egress not allowed", alternatives=["use cached data", "ask operator"],
    )
    assert v.decision == HarnessDecision.DENY
    assert v.risk_band == RiskBand.HIGH
    assert "ask operator" in v.alternatives
