"""v0.9.38 Bug 13 — SUBAGENT pull runs terminate (instead of parking forever).

Two coupled fixes:
  * The harness-grant SUBAGENT observation now uses TERMINAL framing ("proceed
    and COMPLETE; do NOT request more sub-agents"), mirroring the native fleet
    branch — the old "decompose and proceed" wording invited re-requesting.
  * The per-run request cap now DENIES even blocking requests (was ESCALATE →
    suspend), so a capped run continues to a terminal (COMPLETE/FAIL) and
    reconciles, instead of escalate→suspend→approve→resume looping until parked.
"""
from __future__ import annotations

from systemu.runtime.harness_arbiter import arbitrate
from systemu.runtime.harness_policy import HarnessPolicy
from systemu.core.models import HarnessRequest, HarnessKind, HarnessDecision


# ── Part 2: cap is a hard DENY (no indefinite suspend) ──────────────────────

def test_cap_exceeded_blocking_denies_not_escalates():
    policy = HarnessPolicy(max_requests_per_run=8)
    req = HarnessRequest(kind=HarnessKind.SUBAGENT, spec={"tasks": ["t"]},
                         rationale="delegate", blocking=True)
    result = arbitrate(req, policy, {"requests_this_run": 8, "subagent_depth": 0})
    # was ESCALATE (→ suspend → resume → re-request loop, never terminal)
    assert result["verdict"].decision == HarnessDecision.DENY


def test_cap_exceeded_nonblocking_still_denies():
    policy = HarnessPolicy(max_requests_per_run=8)
    req = HarnessRequest(kind=HarnessKind.SUBAGENT, spec={"tasks": ["t"]},
                         rationale="delegate", blocking=False)
    result = arbitrate(req, policy, {"requests_this_run": 9, "subagent_depth": 0})
    assert result["verdict"].decision == HarnessDecision.DENY


def test_cap_not_exceeded_runs_kind_logic():
    # below cap → the cap branch does NOT short-circuit (kind logic decides).
    policy = HarnessPolicy(max_requests_per_run=8)
    req = HarnessRequest(kind=HarnessKind.SUBAGENT, spec={"tasks": ["t"]},
                         rationale="delegate", blocking=True)
    result = arbitrate(req, policy, {"requests_this_run": 2, "subagent_depth": 0})
    # default policy (auto_grant_subagent off) → a new subagent escalates, not the
    # cap DENY. The point: the cap did not fire below the limit.
    assert result["verdict"].decision != HarnessDecision.DENY or \
        "cap" not in (result["verdict"].rationale or "").lower()


# ── Part 1: harness-grant SUBAGENT observation is terminal ──────────────────

class _Ctx:
    def __init__(self):
        self.observations = []

    def add_observation(self, payload, ab):
        self.observations.append(payload)


def test_subagent_grant_observation_is_terminal():
    from systemu.runtime.shadow_runtime import ShadowRuntime
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.vault = None
    rt.config = None
    ctx = _Ctx()
    mat = {"materialised": True, "lease_id": "L",
           "subagent": {"task": "summarise the three reports"}}
    rt._apply_materialised_grant(mat, context=ctx, tools=[], tool_index=[],
                                 current_ab=0, iter_budget=5)
    assert ctx.observations, "no observation emitted for subagent grant"
    obs = ctx.observations[-1]
    msg = obs["message"].lower()
    assert "do not request more sub-agents" in msg, msg
    assert "complete" in msg, msg
    # NOT the old "decompose and proceed" re-delegation invitation
    assert "decompose and proceed" not in msg
    assert obs.get("fleet", {}).get("terminal") is True
