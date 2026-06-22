"""v0.9.38 Bug 12 — request-outcome taxonomy is correct across ALL grant kinds.

Two coupled defects fixed:
  * granted_used was TOOL-only (read outcome["tool"]); MCP grants (namespaced
    tools) + compute/access/skill/subagent (no single tool) were always
    granted_unused. Now resolved per kind via Governor._granted_tool_names,
    with N/A kinds (no materialised tool) never marked unused_grant.
  * premature_request fired for every kind at attempts_before<1; now only for
    kinds where a local attempt is expected (tool/skill) — concrete-gap kinds
    (access/mcp/compute/subagent) requesting immediately are not premature.
"""
from __future__ import annotations

from systemu.runtime.governor import Governor
from systemu.runtime.mcp.sdk.registry_bridge import namespaced_name
from systemu.runtime import failure_classifier as fc


# ── helper: materialised tool names per kind ────────────────────────────────

def test_granted_tool_names_per_kind():
    assert Governor._granted_tool_names({"tool": "geocode"}, "tool") == {"geocode"}
    mcp = {"mcp": {"server_id": "lookup",
                   "tools": [{"name": "resolve"}, {"name": "lookup_id"}]}}
    assert Governor._granted_tool_names(mcp, "mcp") == {
        namespaced_name("lookup", "resolve"),
        namespaced_name("lookup", "lookup_id"),
    }
    # no materialised single tool → N/A (None), never penalised as unused
    for k in ("compute", "access", "skill", "subagent"):
        assert Governor._granted_tool_names({"materialised": True}, k) is None
    assert Governor._granted_tool_names({"mcp": {"server_id": "x", "tools": []}}, "mcp") is None


# ── reconcile_outcomes: MCP grants classify by namespaced tool use ──────────

def test_reconcile_mcp_grant_used():
    used = {namespaced_name("lookup", "resolve_code")}
    rows = [{"request": {"request_id": "m1", "kind": "mcp", "attempts_before": 0},
             "verdict": {"decision": "grant"},
             "outcome": {"mcp": {"server_id": "lookup",
                                 "tools": [{"name": "resolve_code"}]}},
             "execution_id": "e"}]
    ev = Governor.reconcile_outcomes(rows, used, run_success=True)
    assert ev[0]["outcome"] == "granted_used"             # was granted_unused (Bug 12)
    assert ev[0]["pull_failure_category"] != "premature_request"  # mcp@0 not premature


def test_reconcile_mcp_grant_unused():
    rows = [{"request": {"request_id": "m2", "kind": "mcp", "attempts_before": 0},
             "verdict": {"decision": "grant"},
             "outcome": {"mcp": {"server_id": "lookup",
                                 "tools": [{"name": "resolve_code"}]}},
             "execution_id": "e"}]
    ev = Governor.reconcile_outcomes(rows, set(), run_success=True)  # not called
    assert ev[0]["outcome"] == "granted_unused"


# ── N/A kinds: granted (usage indeterminate), never unused_grant ────────────

def test_reconcile_na_kinds_granted_not_unused():
    # attempts_before=2 so the (separate) premature dimension can't confound this.
    for kind in ("compute", "access", "subagent", "skill"):
        rows = [{"request": {"request_id": f"g_{kind}", "kind": kind, "attempts_before": 2},
                 "verdict": {"decision": "grant"}, "outcome": {"materialised": True},
                 "execution_id": "e"}]
        ev = Governor.reconcile_outcomes(rows, set(), run_success=True)
        assert ev[0]["outcome"] == "granted", f"{kind}: {ev[0]['outcome']}"
        assert ev[0]["pull_failure_category"] != "unused_grant", kind


# ── TOOL premature is still caught (the case the metric is meant to flag) ────

def test_reconcile_tool_premature_still_caught():
    rows = [{"request": {"request_id": "t1", "kind": "tool", "attempts_before": 0},
             "verdict": {"decision": "grant"}, "outcome": {"tool": "geocode"},
             "execution_id": "e"}]
    ev = Governor.reconcile_outcomes(rows, {"geocode"}, run_success=True)
    assert ev[0]["outcome"] == "granted_used"                    # used
    assert ev[0]["pull_failure_category"] == "premature_request"  # but requested @0 (tool)


def test_classify_premature_kind_matrix():
    # tool + skill at attempts<1 → premature; concrete-gap kinds → not.
    for k in ("tool", "skill"):
        assert fc.classify_pull_failure(
            attempts_before=0, decision="grant", fallback_ok=None,
            used_after_grant=True, kind=k) == "premature_request"
    for k in ("access", "mcp", "compute", "subagent"):
        assert fc.classify_pull_failure(
            attempts_before=0, decision="grant", fallback_ok=None,
            used_after_grant=True, kind=k) != "premature_request"
