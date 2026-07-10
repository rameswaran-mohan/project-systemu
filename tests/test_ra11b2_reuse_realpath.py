# tests/test_ra11b2_reuse_realpath.py
"""R-A11b-2 Task 5 — end-to-end reuse through the REAL arbitrate→materialise
chain, composed exactly as Seam A does. The invariant's falsification target:
auto-reuse confers no capability the agent could not already invoke."""
import json
from pathlib import Path

import pytest

from systemu.core.models import (
    HarnessDecision, HarnessKind, HarnessRequest, RiskBand, Tool, ToolStatus, ToolType,
)
from systemu.runtime.governor import Governor
from systemu.runtime.discovery_pass import deployed_enabled_catalog, discovery_pass
from systemu.vault.vault import Vault as FileVault


def _cfg():
    return {"auto_grant_tool": False, "max_requests_per_run": 8,
            "max_requests_per_activity": 20}


def _save_deployed(vault, name):
    vault.save_tool(Tool(
        id=f"tool_{name}", name=name, description=f"deployed {name} tool",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True, forged_by_systemu=True))


def _seam_a_arbitrate(gov, vault, req, execution_id, requests_this_run=0):
    """Replicate Seam A's composition: discovery pass → inject ctx/spec →
    arbitrate. Returns (verdict, discovery_result)."""
    ctx = {"requests_this_run": requests_this_run, "subagent_depth": 0,
           "requests_this_activity": 0}
    disc = None
    if req.kind == HarnessKind.TOOL:
        cat = deployed_enabled_catalog(vault)
        disc = discovery_pass((req.spec or {}).get("name", ""), req.rationale or "", cat)
        req.spec["discovery"] = {"searched": disc.searched,
                                 "best_score": disc.best_score, "floor": disc.floor}
        if disc.reuse_tool_id:
            # matches the real Seam A: inject the REQUESTED name (what the arbiter
            # is_reuse checks), not the matched name.
            ctx["enabled_tools"] = [(req.spec or {}).get("name", "")]
            req.spec["reuse_tool_id"] = disc.reuse_tool_id
            req.spec["reuse_score"] = disc.best_score
    verdict = gov.arbitrate(req, context=ctx)
    return verdict, disc


def _tool_req(name, rationale="I need this capability"):
    return HarnessRequest(kind=HarnessKind.TOOL,
                          spec={"name": name, "description": f"forge {name}"},
                          rationale=rationale)


def test_confident_match_is_reused_no_forge(tmp_path, monkeypatch):
    vault = FileVault(root=str(tmp_path))
    _save_deployed(vault, "fetch_weather")
    gov = Governor(config=_cfg())
    import systemu.runtime.governor as govmod
    monkeypatch.setattr(govmod, "forge_proposed_tools",
                        lambda *a, **k: pytest.fail("must not forge on a confident reuse"))

    req = _tool_req("fetch_weather", "fetch the weather forecast")
    verdict, disc = _seam_a_arbitrate(gov, vault, req, "e1")
    assert verdict.decision == HarnessDecision.GRANT
    assert verdict.risk_band == RiskBand.LOW          # reuse is LOW, not a HIGH forge
    assert disc.reuse_tool_id == "tool_fetch_weather"

    out = gov.materialise(req, verdict, vault=vault, config=_cfg(), execution_id="e1")
    assert out["reused"] is True and out["forge_avoided"] is True
    assert out["tool"] == "fetch_weather"
    assert vault.list_tools(status=ToolStatus.PROPOSED) == []   # no PROPOSED record

    rows = [json.loads(l) for l in
            gov.ledger_path("e1", vault).read_text(encoding="utf-8").splitlines()]
    assert any(r.get("outcome", {}).get("reused") for r in rows)   # HIT audit


def test_no_match_escalates_and_writes_miss_audit(tmp_path):
    vault = FileVault(root=str(tmp_path))
    _save_deployed(vault, "send_email")           # unrelated tool present
    gov = Governor(config=_cfg())
    req = _tool_req("compress_pdf", "compress a pdf into a smaller file")
    verdict, disc = _seam_a_arbitrate(gov, vault, req, "e2")
    # byte-identical to today's forge path: HIGH ESCALATE (blocking default True)
    assert verdict.decision == HarnessDecision.ESCALATE
    assert verdict.risk_band == RiskBand.HIGH
    assert disc.reuse_tool_id is None
    assert "reuse_tool_id" not in req.spec           # nothing injected on a miss

    # MISS audit (the Seam A manual-append; replicate that append here)
    gov._ledger_append(
        gov._ledger_entry(req, verdict,
                          {"discovery_miss": {"searched": disc.searched,
                                              "best_score": disc.best_score,
                                              "floor": disc.floor}}, "e2"),
        vault=vault, execution_id="e2")
    rows = [json.loads(l) for l in
            gov.ledger_path("e2", vault).read_text(encoding="utf-8").splitlines()]
    miss = [r for r in rows if r.get("outcome", {}).get("discovery_miss")]
    assert miss and miss[0]["outcome"]["discovery_miss"]["best_score"] < disc.floor


def test_mcp_only_match_is_never_reused(tmp_path):
    """A name that exists ONLY as an MCP enabled tool (connections state), NOT as a
    vault Tool, must never be reused (Rider 1 — CAP-3 vector closed)."""
    vault = FileVault(root=str(tmp_path))
    from systemu.runtime.mcp import connections
    # register an MCP tool named exactly like the request; NO vault Tool exists
    connections.set_tool_enabled(vault, "https://x", "fetch_weather", True,
                                 description="fetch weather via MCP")
    gov = Governor(config=_cfg())
    req = _tool_req("fetch_weather", "fetch the weather forecast")
    verdict, disc = _seam_a_arbitrate(gov, vault, req, "e3")
    assert disc.searched == 0                    # MCP tool not in the catalog
    assert disc.reuse_tool_id is None
    assert verdict.decision == HarnessDecision.ESCALATE   # forge path, not reuse


def test_weak_match_falls_through(tmp_path):
    vault = FileVault(root=str(tmp_path))
    _save_deployed(vault, "send_email")
    gov = Governor(config=_cfg())
    req = _tool_req("render_svg_chart", "render an svg bar chart from data")
    verdict, disc = _seam_a_arbitrate(gov, vault, req, "e4")
    assert disc.best_score < disc.floor
    assert disc.reuse_tool_id is None
    assert verdict.decision == HarnessDecision.ESCALATE


def test_reuse_still_obeys_the_run_cap(tmp_path):
    """A reuse flows the SAME arbitrate call, so the per-run cap DENIES it just
    like a forge (v0.9.47: reuse never bypasses a cap)."""
    vault = FileVault(root=str(tmp_path))
    _save_deployed(vault, "fetch_weather")
    gov = Governor(config=_cfg())
    req = _tool_req("fetch_weather", "fetch the weather forecast")
    # requests_this_run at the cap (max_requests_per_run=8) → hard DENY
    verdict, disc = _seam_a_arbitrate(gov, vault, req, "e5", requests_this_run=8)
    assert disc.reuse_tool_id == "tool_fetch_weather"   # discovery still found it
    assert verdict.decision == HarnessDecision.DENY      # but the cap wins
    assert getattr(verdict, "cap_exceeded", False) is True


def test_low_grant_branch_only_fires_for_kind_tool(tmp_path):
    """The invariant: injecting enabled_tools grants ONLY a kind=tool request.
    A SKILL request carrying the same enabled_tools name is NOT tool-granted."""
    from systemu.runtime.harness_arbiter import arbitrate
    from systemu.runtime.harness_policy import HarnessPolicy
    policy = HarnessPolicy.from_config(_cfg())
    ctx = {"enabled_tools": ["fetch_weather"], "requests_this_run": 0}
    # kind=tool with the injected name → LOW GRANT (reuse)
    tool_req = HarnessRequest(kind=HarnessKind.TOOL, spec={"name": "fetch_weather"})
    v_tool = arbitrate(tool_req, policy, ctx)["verdict"]
    assert v_tool.decision == HarnessDecision.GRANT
    assert v_tool.risk_band == RiskBand.LOW
    # kind=skill with the SAME name in enabled_tools → NOT a tool grant path
    skill_req = HarnessRequest(kind=HarnessKind.SKILL, spec={"name": "fetch_weather"})
    v_skill = arbitrate(skill_req, policy, ctx)["verdict"]
    assert not (v_skill.decision == HarnessDecision.GRANT
                and v_skill.risk_band == RiskBand.LOW
                and "already enabled" in (v_skill.rationale or ""))


def test_high_overlap_nonexact_name_is_not_reused(tmp_path):
    """Fix #4: exact-name-only reuse. A request whose name is NOT identical to a
    deployed tool — even with heavy token/description overlap — must NOT auto-reuse.
    It falls through to the honest forge/ESCALATE path (the fuzzy near-match is
    R-CAP1's job, with operator confirm; a description-driven fuzzy auto-reuse would
    be the CAP-3 keyword-stuffing vector)."""
    vault = FileVault(root=str(tmp_path))
    # deployed tool named send_email; the request asks to forge 'email_sender'
    vault.save_tool(Tool(
        id="tool_send_email", name="send_email",
        description="send an email message email email email to a recipient",
        tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
        enabled=True, forged_by_systemu=True))
    gov = Governor(config=_cfg())
    req = _tool_req("email_sender", "send an email email email message to someone")
    verdict, disc = _seam_a_arbitrate(gov, vault, req, "e7")
    assert disc.reuse_tool_id is None, "a non-exact name must never auto-reuse"
    assert "reuse_tool_id" not in req.spec
    assert verdict.decision == HarnessDecision.ESCALATE   # honest forge path
    # best_score is still recorded as the audit near-match signal
    assert disc.best_score >= 0.0
