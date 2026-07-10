# tests/test_ra11b2_ac_gate.py
"""R-A11b-2 AC gate — the load-bearing invariants as first-class assertions."""
import inspect

from systemu.runtime.governor import Governor
from systemu.runtime.shadow_runtime import ShadowRuntime


def test_reuse_never_writes_a_proposed_record_or_forges():
    """v0.9.47 Fix #1: the reuse branch must return BEFORE save_tool/forge."""
    src = inspect.getsource(Governor._reuse_existing_tool)
    assert "save_tool" not in src
    assert "forge_proposed_tools" not in src


def test_reuse_reverifies_deployed_enabled_not_rejected():
    src = inspect.getsource(Governor._reuse_existing_tool)
    assert "ToolStatus.DEPLOYED" in src
    assert "enabled" in src
    assert "forge_rejected" in src


def test_seam_a_keeps_the_same_arbitrate_call():
    """Reuse flows the SAME arbitrate → both v0.9.47 caps count (Seam A adds NO
    new arbitrate call). execute() has exactly TWO identical-text arbitrate sites
    and BOTH pre-date R-A11b-2: the kind=tool forge site (~:5936, Seam A's home,
    where reuse rides the same call) and the missing-required INPUT site (~:6210,
    excluded from Seam A per the plan's grounding). This count was 2 at the base
    commit and MUST stay 2 — a 3rd literal would mean reuse got its own arbitrate
    call that bypasses the caps (the exact v0.9.47 regression this guards)."""
    src = inspect.getsource(ShadowRuntime.execute)
    assert src.count("_verdict = _gov.arbitrate(_req, context=_arb_ctx)") == 2


def test_discovery_is_one_pass_not_a_loop():
    """ONE deterministic pass — no iterative search→forge→search."""
    from systemu.runtime import discovery_pass as dp
    src = inspect.getsource(dp)
    # a single rank call, no while/for-driven re-ranking
    assert src.count("rank_tools_scored(") == 1
    assert "while" not in src


def test_cap_deny_is_hard_not_escalate_suspend():
    """v0.9.38 Bug 13: a cap-exceeded reuse is a HARD DENY, never a suspend."""
    from systemu.runtime.harness_arbiter import arbitrate
    from systemu.runtime.harness_policy import HarnessPolicy
    from systemu.core.models import HarnessDecision, HarnessKind, HarnessRequest
    policy = HarnessPolicy.from_config({"max_requests_per_run": 2})
    ctx = {"enabled_tools": ["fetch_weather"], "requests_this_run": 2}
    req = HarnessRequest(kind=HarnessKind.TOOL, spec={"name": "fetch_weather"})
    v = arbitrate(req, policy, ctx)["verdict"]
    assert v.decision == HarnessDecision.DENY
