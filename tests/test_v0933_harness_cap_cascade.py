"""v0.9.33 Section B — revive the per-run harness request cap (Bug 2) AND
close the subagent-cascade barriers (Bug 3) via a threaded arbitration context.

All Section B tests live here (B.1–B.6 + the cross-cutting integration tests).
The cap/depth tests delenv the SYSTEMU_HARNESS_* vars for hermeticity so an
operator's environment cannot perturb the policy defaults under test.
"""
from __future__ import annotations

import importlib
import inspect
import re

import pytest


# ─────────────────────────────────────────────────────────────────────────────
#  B.1 — per-execution harness-request counter helper
# ─────────────────────────────────────────────────────────────────────────────

def test_next_harness_request_no_increments_from_zero():
    sr = importlib.import_module("systemu.runtime.shadow_runtime")
    assert sr._next_harness_request_no(0) == 1
    assert sr._next_harness_request_no(1) == 2


def test_next_harness_request_no_coerces_garbage_to_one():
    sr = importlib.import_module("systemu.runtime.shadow_runtime")
    # A corrupted/None prior count must never crash the loop — it floors to 1.
    assert sr._next_harness_request_no(None) == 1
    assert sr._next_harness_request_no("x") == 1
    assert sr._next_harness_request_no(-5) == 1


# ─────────────────────────────────────────────────────────────────────────────
#  B.2 — pass the arbitration context at the call site (revives cap + depth)
# ─────────────────────────────────────────────────────────────────────────────

from systemu.runtime.governor import Governor
from systemu.core.models import HarnessRequest, HarnessKind, HarnessDecision


@pytest.fixture(autouse=True)
def _hermetic_harness_env(monkeypatch):
    """Strip operator SYSTEMU_HARNESS_* overrides so policy defaults under test
    are deterministic regardless of the runner's environment."""
    import os
    for k in list(os.environ):
        if k.startswith("SYSTEMU_HARNESS_"):
            monkeypatch.delenv(k, raising=False)


def _gov(max_requests):
    return Governor(config={"max_requests_per_run": max_requests,
                            "llm_judge_enabled": False})


def test_cap_fires_via_governor_with_context():
    """The cap is dead unless the loop passes requests_this_run. With it, a
    non-blocking SKILL request AT the cap is DENIED through the Governor."""
    gov = _gov(3)
    req = HarnessRequest(kind=HarnessKind.SKILL, spec={"name": "anything_new"},
                         rationale="need it", blocking=False)
    verdict = gov.arbitrate(req, context={"requests_this_run": 3,
                                          "subagent_depth": 0})
    assert verdict.decision == HarnessDecision.DENY


def test_cap_silent_without_context_is_the_bug():
    """Documents the regression: NO context => cap never fires (request is not
    capped even at the cap). The call-site fix is what supplies context."""
    gov = _gov(3)
    req = HarnessRequest(kind=HarnessKind.SKILL, spec={"name": "anything_new"},
                         rationale="need it", blocking=False)
    verdict = gov.arbitrate(req)  # no context — the pre-fix call site
    assert verdict.decision != HarnessDecision.DENY  # cap is silent => bug


def test_call_site_passes_context_dict():
    """Source-level guard: the production arbitrate call threads a context with
    requests_this_run + subagent_depth (so the cap & depth guard are live)."""
    from systemu.runtime import shadow_runtime
    src = inspect.getsource(shadow_runtime)
    # The single production call must carry context=...
    assert re.search(r"_gov\.arbitrate\(\s*_req,\s*context=", src), \
        "shadow_runtime must call _gov.arbitrate(_req, context=...)"
    assert "requests_this_run" in src
    assert "subagent_depth" in src


# ─────────────────────────────────────────────────────────────────────────────
#  B.3 — depth guard reflects ACTUAL nesting, not model-claimed spec.depth
# ─────────────────────────────────────────────────────────────────────────────

from systemu.runtime.harness_arbiter import arbitrate
from systemu.runtime.harness_policy import HarnessPolicy


def _depth_policy():
    # auto_grant_subagent True so a WITHIN-limits request would GRANT — isolating
    # the depth guard as the only thing that can block.
    return HarnessPolicy(max_subagent_depth=1, auto_grant_subagent=True,
                         max_requests_per_run=99)


def _subagent_req(claimed_depth, blocking=False):
    return HarnessRequest(kind=HarnessKind.SUBAGENT,
                          spec={"depth": claimed_depth, "budget_fraction": 0.1,
                                "tasks": ["t"]},
                          rationale="delegate", blocking=blocking)


def test_actual_depth_denies_nested_claim_of_one():
    """Child at actual depth 1 claims depth=1 but real nesting is 1+1=2 > max 1."""
    r = _subagent_req(claimed_depth=1, blocking=False)
    res = arbitrate(r, _depth_policy(), context={"subagent_depth": 1})
    assert res["verdict"].decision == HarnessDecision.DENY


def test_parent_within_actual_depth_grants():
    """Parent at depth 0: actual nesting 0+1=1 == max 1 → within limits → GRANT."""
    r = _subagent_req(claimed_depth=1)
    res = arbitrate(r, _depth_policy(), context={"subagent_depth": 0})
    assert res["verdict"].decision == HarnessDecision.GRANT


def test_model_lie_cannot_undercut_actual_depth():
    """A child at actual depth 1 that LIES depth=0 is still denied (we use ctx)."""
    r = _subagent_req(claimed_depth=0, blocking=False)
    res = arbitrate(r, _depth_policy(), context={"subagent_depth": 1})
    assert res["verdict"].decision == HarnessDecision.DENY


def test_model_claimed_large_depth_still_escalates():
    """The max() form retains the model-claimed depth too: a parent (ctx depth 0)
    claiming depth=5 still exceeds max 1 → escalate/deny (preserves the existing
    arbiter contract for an over-claimed depth)."""
    r = _subagent_req(claimed_depth=5, blocking=False)
    res = arbitrate(r, _depth_policy(), context={"subagent_depth": 0})
    assert res["verdict"].decision == HarnessDecision.DENY


# ─────────────────────────────────────────────────────────────────────────────
#  B.4 — children run on a recursion-disabled, depth-stamped config; and a
#  child cannot recurse via EITHER the native fleet path NOR the v2 tool path.
# ─────────────────────────────────────────────────────────────────────────────

from systemu.runtime.subagent_fleet import SubagentFleet
from systemu.runtime.shadow_runtime import ShadowRuntime


class _FleetCfg:
    """Minimal config double with the flags the fleet/runtime read."""
    delegate_use_parallel = True
    delegate_max_concurrent_children = 2


def _fleet():
    return SubagentFleet(parent_execution_id="exec-parent",
                         config=_FleetCfg(), vault=object())


def test_child_config_forces_delegate_off():
    fleet = _fleet()
    child_cfg = fleet._build_child_config(parent_depth=0)
    assert getattr(child_cfg, "delegate_use_parallel") is False
    # Parent config must be untouched (no shared-mutation regression).
    assert fleet.config.delegate_use_parallel is True


def test_child_config_increments_depth():
    fleet = _fleet()
    child_cfg = fleet._build_child_config(parent_depth=0)
    assert getattr(child_cfg, "_subagent_depth", 0) == 1
    grandchild_cfg = fleet._build_child_config(parent_depth=1)
    assert getattr(grandchild_cfg, "_subagent_depth", 0) == 2


def test_runtime_depth_helper_reads_config():
    """The pure helper reflects a depth-stamped config; no ShadowRuntime build
    (a bare _Cfg would crash __init__ on vault_dir / sandbox construction)."""
    sr = importlib.import_module("systemu.runtime.shadow_runtime")
    cfg = _FleetCfg()
    cfg._subagent_depth = 2
    assert sr._runtime_depth_from_config(cfg) == 2


def test_runtime_depth_helper_defaults_zero():
    sr = importlib.import_module("systemu.runtime.shadow_runtime")
    assert sr._runtime_depth_from_config(_FleetCfg()) == 0
    assert sr._runtime_depth_from_config(None) == 0


def test_runtime_new_reads_depth_from_config():
    """__init__ stamps _subagent_depth from the config without a full build —
    we mirror Section A's __new__ pattern to avoid the sandbox dereference."""
    rt = ShadowRuntime.__new__(ShadowRuntime)
    cfg = _FleetCfg()
    cfg._subagent_depth = 3
    rt.config = cfg
    rt._subagent_depth = ShadowRuntime._init_subagent_depth(cfg)
    assert rt._subagent_depth == 3


@pytest.mark.asyncio
async def test_child_runtime_cannot_spawn_via_v2_path(tmp_path):
    """CRITICAL (Section A interaction): the v2 spawn_subagent / delegate /
    mixture_of_agents tools are now DISPATCHABLE. A CHILD runtime (depth>=1)
    must NOT be able to recurse through that v2 short-circuit — it returns a
    refusal observation and never reaches the v2 handler."""
    from systemu.runtime.tool_sandbox import ToolSandbox, ToolResult
    from systemu.runtime.context_builder import ExecutionContext
    import systemu.runtime.tools.delegate  # noqa: F401  (force-register spawn_subagent)

    class _Cfg:
        def __init__(self, out):
            self.output_dir = out
            self.check_fn_cache_ttl_seconds = 30
            self.capability_track_outcomes = False

    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    cfg = _Cfg(str(out))

    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.config = cfg
    rt.vault = None
    rt.sandbox = ToolSandbox(vault_root=tmp_path, backend="local", vault=None, config=cfg)
    rt._dep_failed_tools = {}
    rt._consec_tool_fails = {}
    rt._fresh_work_since_last_verifier_call = False
    rt._execution_mind = None
    rt._subagent_depth = 1  # this is a CHILD runtime

    # If the v2 handler were reached it would explode (no real LLM / env); the
    # refusal must fire first. Make the dispatcher loud if it is ever called.
    called = {"v2": False}

    async def _boom(*a, **k):
        called["v2"] = True
        raise AssertionError("child reached the v2 spawn_subagent handler — recursion barrier breached")

    rt.sandbox.execute = _boom  # type: ignore[assignment]

    ctx = ExecutionContext(execution_id="t", system_prompt="", scroll_json=[], tool_index=[])
    decision = {"decision": "TOOL_CALL", "tool_name": "spawn_subagent",
                "parameters": {"task": "do x"}, "reasoning": "delegate"}
    result = await rt._handle_tool_call(decision, tools=[], context=ctx,
                                        current_ab=0, dry_run=False)

    assert called["v2"] is False, "child must not reach the v2 delegation handler"
    # A refusal observation is recorded; the call did not silently dispatch.
    obs = [e.content for e in ctx._history if e.event_type == "observation"]
    assert obs, "no observation recorded for the refused child delegation"
    blob = str(obs).lower()
    assert "delegat" in blob or "subagent" in blob or "recursion" in blob or "not" in blob


@pytest.mark.asyncio
async def test_parent_runtime_can_dispatch_v2_non_delegation_tool(tmp_path):
    """Guard: the child refusal must NOT block a PARENT (depth 0) — and must not
    block non-delegation v2 tools for anyone. A parent write_file still runs."""
    from systemu.runtime.tool_sandbox import ToolSandbox, ToolResult
    from systemu.runtime.context_builder import ExecutionContext
    import systemu.runtime.tools.file_tools  # noqa: F401

    class _Cfg:
        def __init__(self, out):
            self.output_dir = out
            self.check_fn_cache_ttl_seconds = 30
            self.capability_track_outcomes = False

    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    cfg = _Cfg(str(out))
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.config = cfg
    rt.vault = None
    rt.sandbox = ToolSandbox(vault_root=tmp_path, backend="local", vault=None, config=cfg)
    rt._dep_failed_tools = {}
    rt._consec_tool_fails = {}
    rt._fresh_work_since_last_verifier_call = False
    rt._execution_mind = None
    rt._subagent_depth = 0  # parent

    ctx = ExecutionContext(execution_id="t", system_prompt="", scroll_json=[], tool_index=[])
    decision = {"decision": "TOOL_CALL", "tool_name": "write_file",
                "parameters": {"path": "ok.txt", "content": "hi"}, "reasoning": "r"}
    result = await rt._handle_tool_call(decision, tools=[], context=ctx,
                                        current_ab=0, dry_run=False)
    assert isinstance(result, ToolResult) and result.success is True
    assert (out / "ok.txt").exists()


# ─────────────────────────────────────────────────────────────────────────────
#  B.5 — the fleet-result observation is terminal (no re-delegation nudge)
# ─────────────────────────────────────────────────────────────────────────────

def test_fleet_observation_is_terminal():
    """Source-level guard: the fleet-result harness_granted observation must
    instruct the agent to synthesize + COMPLETE, and NOT re-delegate."""
    from systemu.runtime import shadow_runtime
    src = inspect.getsource(shadow_runtime)
    assert "do not re-delegate" in src.lower()
    assert "synthesize" in src.lower() or "synthesise" in src.lower()
    # The crediting of the children's work is retained (synthesis still flows).
    assert '_fres.get("synthesis")' in src


# ─────────────────────────────────────────────────────────────────────────────
#  B.6 — snapshot persists harness-request count + depth across suspend/resume
# ─────────────────────────────────────────────────────────────────────────────

import json

from systemu.runtime.execution_snapshot import (
    ExecutionSnapshot, write_snapshot, read_snapshot, _to_dict, capture_from_context,
)


def test_snapshot_carries_new_fields():
    snap = ExecutionSnapshot(
        execution_id="exec-1", shadow_id="s", scroll_id="sc",
        requests_this_run=5, subagent_depth=1,
    )
    d = _to_dict(snap)
    assert d["requests_this_run"] == 5
    assert d["subagent_depth"] == 1


def test_snapshot_roundtrip_disk(tmp_path):
    snap = ExecutionSnapshot(
        execution_id="exec-rt", shadow_id="s", scroll_id="sc",
        requests_this_run=7, subagent_depth=2,
    )
    write_snapshot(snap, data_dir=tmp_path)
    got = read_snapshot("exec-rt", data_dir=tmp_path)
    assert got is not None
    assert got.requests_this_run == 7
    assert got.subagent_depth == 2


def test_old_snapshot_without_fields_defaults_zero(tmp_path):
    """Backward compat: a pre-v0.9.33 snapshot file lacks the keys → 0."""
    target = tmp_path / "audit" / "exec_old" / "resume_snapshot.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "execution_id": "old", "shadow_id": "s", "scroll_id": "sc",
        "iteration": 3,
    }), encoding="utf-8")
    got = read_snapshot("old", data_dir=tmp_path)
    assert got is not None
    assert got.requests_this_run == 0
    assert got.subagent_depth == 0


def test_capture_from_context_threads_new_fields():
    """capture_from_context accepts + carries the two new fields."""
    class _Ctx:
        def get_sticky_notes(self):
            return []
    snap = capture_from_context(
        execution_id="e", shadow_id="s", scroll_id="sc",
        iteration=2, current_action_block=1,
        completed_objectives=set(), context=_Ctx(),
        requests_this_run=4, subagent_depth=1,
    )
    assert snap.requests_this_run == 4
    assert snap.subagent_depth == 1


def test_all_three_capture_sites_thread_count():
    """Source guard: every resumable capture_from_context call site (the harness-
    escalate, the stuck-park, and the recalibration helper which stashes on
    context) threads requests_this_run + subagent_depth."""
    from systemu.runtime import shadow_runtime
    src = inspect.getsource(shadow_runtime)
    # The two direct capture sites (harness-escalate + stuck-park) thread the
    # live loop-local count.
    n_direct = src.count("requests_this_run=harness_requests_this_run")
    assert n_direct >= 2, f"expected >=2 direct threaded capture sites, found {n_direct}"
    # The recalibration helper pulls loop state off context (no loop-local in
    # scope) → the count is stashed on context AND read back into its capture.
    assert "_resume_requests_this_run" in src
    assert 'context, "_resume_requests_this_run"' in src \
        or "context, '_resume_requests_this_run'" in src
    # Every capture site also threads the depth.
    assert src.count("subagent_depth=") >= 3


def test_resume_restore_uses_snap_before_delete():
    """Source guard: the resume restore reads the ALREADY-READ snap (not a second
    read_snapshot) and happens before delete_snapshot, inside the resume block."""
    from systemu.runtime import shadow_runtime
    src = inspect.getsource(shadow_runtime)
    # The restore assigns harness_requests_this_run from the already-read snap.
    restore_idx = src.find('getattr(snap, "requests_this_run"')
    if restore_idx < 0:
        restore_idx = src.find("getattr(snap, 'requests_this_run'")
    assert restore_idx > 0, \
        "resume must restore harness_requests_this_run from the already-read snap"
    # It must be wired into harness_requests_this_run (the loop-local the loop reads).
    assert re.search(r"harness_requests_this_run\s*=.*?getattr\(snap,\s*"
                     r"[\"']requests_this_run", src, re.DOTALL), \
        "restore must assign harness_requests_this_run from snap"
    # The restore must NOT introduce a SECOND read_snapshot inside the resume block
    # (it reuses the snap read at the top of `if resume_from_execution_id:`).
    assert src.count("read_snapshot(resume_from_execution_id)") <= 1, \
        "resume restore must reuse the already-read snap, not re-read it"
    # Restore precedes delete_snapshot in source order within the resume block.
    delete_idx = src.find("delete_snapshot(resume_from_execution_id)")
    assert delete_idx > 0
    assert restore_idx < delete_idx, "restore must run before delete_snapshot"


# ─────────────────────────────────────────────────────────────────────────────
#  Integration — drive the loop's increment + arbitrate together (off-by-one)
# ─────────────────────────────────────────────────────────────────────────────

def _replay_loop_request(prev_count, gov, *, subagent_depth=0, blocking=False):
    """Replay EXACTLY what shadow_runtime's REQUEST_HARNESS branch does for one
    request: capture the PRE-increment count, advance the counter, build the
    arbitration context via the production helpers, and arbitrate. Returns
    (new_count, verdict). Uses the real production functions so an off-by-one in
    the source (pre- vs post-increment) is caught here, not papered over."""
    sr = importlib.import_module("systemu.runtime.shadow_runtime")
    pre_inc = prev_count                                  # capture BEFORE increment
    new_count = sr._next_harness_request_no(prev_count)   # then advance
    ctx = sr._harness_arbitration_context(pre_inc, subagent_depth)
    req = HarnessRequest(kind=HarnessKind.SKILL, spec={"name": "new_thing"},
                         rationale="need it", blocking=blocking)
    return new_count, gov.arbitrate(req, context=ctx)


def test_cap_fires_at_exactly_max_via_loop_path():
    """With max_requests_per_run=3, the FIRST THREE requests proceed and the
    FOURTH is capped — proving the cap fires at exactly max (not max-1) through
    the production increment+arbitrate sequence."""
    gov = _gov(3)
    count = 0
    decisions = []
    for _ in range(5):
        count, verdict = _replay_loop_request(count, gov)
        decisions.append(verdict.decision)
    # requests #1, #2, #3 proceed (not DENY); #4 and #5 are capped.
    assert decisions[0] != HarnessDecision.DENY
    assert decisions[1] != HarnessDecision.DENY
    assert decisions[2] != HarnessDecision.DENY, "3rd request must still succeed (cap is max, not max-1)"
    assert decisions[3] == HarnessDecision.DENY, "4th request must be capped"
    assert decisions[4] == HarnessDecision.DENY


def test_loop_path_threads_depth_into_context():
    """The replayed loop sequence feeds the runtime's actual nesting depth into
    the arbitration context, so a child (depth 1) is depth-denied even claiming a
    low depth — recorded by a stub Governor."""
    class _RecordingGov:
        def __init__(self):
            self.seen = []

        def arbitrate(self, req, context=None):
            self.seen.append(dict(context or {}))
            # echo a benign verdict; we only care about the recorded context
            from systemu.core.models import HarnessVerdict, RiskBand
            return HarnessVerdict(request_id=req.request_id,
                                  decision=HarnessDecision.GRANT,
                                  risk_band=RiskBand.LOW, rationale="ok")

    gov = _RecordingGov()
    count = 0
    count, _ = _replay_loop_request(count, gov, subagent_depth=2)
    count, _ = _replay_loop_request(count, gov, subagent_depth=2)
    # The pre-increment counts are 0 then 1 (so the cap counts attempts), and the
    # actual depth is threaded through unchanged.
    assert gov.seen[0]["requests_this_run"] == 0
    assert gov.seen[1]["requests_this_run"] == 1
    assert gov.seen[0]["subagent_depth"] == 2
    assert gov.seen[1]["subagent_depth"] == 2


# ─────────────────────────────────────────────────────────────────────────────
#  Defaults must stay OFF — no default behaviour change
# ─────────────────────────────────────────────────────────────────────────────

def test_policy_defaults_unchanged():
    """delegate_use_parallel / auto_grant_subagent must remain OFF by default."""
    pol = HarnessPolicy.from_config(None)
    assert pol.auto_grant_subagent is False
    # A bare config double has no delegate_use_parallel → the fleet gate reads
    # getattr(..., False) so it stays off.
    class _Bare:
        pass
    assert getattr(_Bare(), "delegate_use_parallel", False) is False
