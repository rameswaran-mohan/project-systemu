# tests/test_ra11b2_provision_reuse.py
"""R-A11b-2 Task 3 — Governor._provision_tool reuse branch + TOCTOU re-verify.
REAL FileVault + real Tool records + the REAL Governor.materialise chain
(no synthetic verdict dicts beyond the operator-authority GRANT verdict)."""
from pathlib import Path

import pytest

from systemu.core.models import (
    HarnessDecision, HarnessKind, HarnessRequest, HarnessVerdict, RiskBand,
    Tool, ToolStatus, ToolType,
)
from systemu.runtime.governor import Governor
from systemu.vault.vault import Vault as FileVault


def _cfg():
    return {"auto_grant_tool": False, "max_requests_per_run": 8,
            "max_requests_per_activity": 20}


def _deployed_tool(name="fetch_weather", enabled=True, forge_rejected=False):
    return Tool(
        id=f"tool_{name}", name=name, description=f"deployed {name}",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=enabled, forged_by_systemu=True, forge_rejected=forge_rejected,
    )


def _grant_low(lease_id="lease_x"):
    return HarnessVerdict(request_id="hreq", decision=HarnessDecision.GRANT,
                          risk_band=RiskBand.LOW, rationale="reuse", lease_id=lease_id)


def _tool_req(name, *, reuse_tool_id=None, reuse_score=None):
    spec = {"name": name, "description": f"forge {name}"}
    if reuse_tool_id is not None:
        spec["reuse_tool_id"] = reuse_tool_id
    if reuse_score is not None:
        spec["reuse_score"] = reuse_score
    return HarnessRequest(kind=HarnessKind.TOOL, spec=spec, rationale="need it")


def test_reuse_hit_no_forge_no_proposed(tmp_path, monkeypatch):
    vault = FileVault(root=str(tmp_path))
    vault.save_tool(_deployed_tool("fetch_weather"))
    gov = Governor(config=_cfg())

    # If forge_proposed_tools were called, fail loudly — reuse must not forge.
    import systemu.runtime.governor as govmod
    monkeypatch.setattr(govmod, "forge_proposed_tools",
                        lambda *a, **k: pytest.fail("forge must NOT run on reuse"))

    req = _tool_req("fetch_weather", reuse_tool_id="tool_fetch_weather", reuse_score=12.0)
    out = gov.materialise(req, _grant_low(), vault=vault, config=_cfg(),
                          execution_id="exec1")

    assert out["materialised"] is True
    assert out["reused"] is True
    assert out["forge_avoided"] is True
    assert out["tool"] == "fetch_weather"
    assert out["tool_id"] == "tool_fetch_weather"
    assert out["lease_id"]
    # NO new PROPOSED tool was written — the only tool is the DEPLOYED one.
    proposed = vault.list_tools(status=ToolStatus.PROPOSED)
    assert proposed == []


def test_reuse_hit_writes_audit_ledger_row(tmp_path):
    vault = FileVault(root=str(tmp_path))
    vault.save_tool(_deployed_tool("fetch_weather"))
    gov = Governor(config=_cfg())
    req = _tool_req("fetch_weather", reuse_tool_id="tool_fetch_weather", reuse_score=12.0)
    gov.materialise(req, _grant_low(), vault=vault, config=_cfg(), execution_id="exec2")

    import json
    rows = [json.loads(l) for l in
            gov.ledger_path("exec2", vault).read_text(encoding="utf-8").splitlines()]
    hit = [r for r in rows if r.get("outcome", {}).get("reused")]
    assert hit, "reuse HIT must appear in the harness ledger"
    assert hit[0]["outcome"]["forge_avoided"] is True
    assert hit[0]["request"]["spec"]["reuse_tool_id"] == "tool_fetch_weather"
    assert hit[0]["request"]["spec"]["reuse_score"] == 12.0


def test_stale_disabled_match_falls_through_to_forge(tmp_path, monkeypatch):
    vault = FileVault(root=str(tmp_path))
    vault.save_tool(_deployed_tool("fetch_weather", enabled=False))  # Gate-3 disabled now
    gov = Governor(config=_cfg())
    called = {"forged": False}
    import systemu.runtime.governor as govmod

    def _fake_forge(activity, config, vlt):
        called["forged"] = True
        return [_deployed_tool("fetch_weather")]  # pretend a fresh forge produced a tool
    monkeypatch.setattr(govmod, "forge_proposed_tools", _fake_forge)

    req = _tool_req("fetch_weather", reuse_tool_id="tool_fetch_weather")
    out = gov.materialise(req, _grant_low(), vault=vault, config=_cfg(),
                          execution_id="exec3")
    # Fix #6: a stale reuse target must NOT forge under the LOW reuse-grant (that
    # would deploy unreviewed code without the HIGH forge-review). It signals
    # not-materialised so the agent re-requests → a fresh forge arbitration.
    assert called["forged"] is False, "a stale reuse must NOT forge under the reuse grant"
    assert out["materialised"] is False
    assert out["reason"] == "reuse_target_stale"


def test_stale_forge_rejected_match_falls_through(tmp_path, monkeypatch):
    vault = FileVault(root=str(tmp_path))
    vault.save_tool(_deployed_tool("fetch_weather", forge_rejected=True))
    gov = Governor(config=_cfg())
    called = {"forged": False}
    import systemu.runtime.governor as govmod
    monkeypatch.setattr(govmod, "forge_proposed_tools",
                        lambda *a, **k: called.__setitem__("forged", True) or [])
    req = _tool_req("fetch_weather", reuse_tool_id="tool_fetch_weather")
    out = gov.materialise(req, _grant_low(), vault=vault, config=_cfg(), execution_id="exec4")
    assert called["forged"] is False   # a forge_rejected reuse target → NOT forged under LOW
    assert out["materialised"] is False and out["reason"] == "reuse_target_stale"


def test_missing_id_falls_through(tmp_path, monkeypatch):
    vault = FileVault(root=str(tmp_path))  # empty vault — the id resolves to nothing
    gov = Governor(config=_cfg())
    called = {"forged": False}
    import systemu.runtime.governor as govmod
    monkeypatch.setattr(govmod, "forge_proposed_tools",
                        lambda *a, **k: called.__setitem__("forged", True) or [])
    req = _tool_req("ghost_tool", reuse_tool_id="tool_ghost")
    out = gov.materialise(req, _grant_low(), vault=vault, config=_cfg(), execution_id="exec5")
    assert called["forged"] is False   # a 404 reuse id → NOT forged under LOW
    assert out["materialised"] is False and out["reason"] == "reuse_target_stale"


def test_no_reuse_id_is_unchanged_forge(tmp_path, monkeypatch):
    vault = FileVault(root=str(tmp_path))
    gov = Governor(config=_cfg())
    called = {"forged": False}
    import systemu.runtime.governor as govmod
    monkeypatch.setattr(govmod, "forge_proposed_tools",
                        lambda *a, **k: called.__setitem__("forged", True) or [_deployed_tool("brand_new")])
    req = _tool_req("brand_new")  # no reuse_tool_id
    gov.materialise(req, _grant_low(), vault=vault, config=_cfg(), execution_id="exec6")
    assert called["forged"] is True
