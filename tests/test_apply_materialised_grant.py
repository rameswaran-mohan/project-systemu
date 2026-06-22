"""Task 3 (harness grant-resume) — shared materialise-apply helper.

`ShadowRuntime._apply_materialised_grant(self, mat, *, context, tools,
tool_index, current_ab, iter_budget) -> int` is the extracted body of the
autonomous GRANT apply-block. It branches on the materialise dict:
  * TOOL  → deploy + append to live tools / tool_index, observation
  * COMPUTE → bump iter_budget (clamped 0..100), observation
  * SKILL / ACCESS / SUBAGENT → observation
  * failure (materialised==False) → harness_grant_failed observation
and returns the possibly-updated iter_budget.

These tests pin the two behaviours the inline block had so the refactor is
provably behaviour-preserving.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from systemu.runtime.shadow_runtime import ShadowRuntime


class _FakeContext:
    """Records add_observation calls (mirrors ContextBuilder.add_observation)."""

    def __init__(self) -> None:
        self.observations: List[Dict[str, Any]] = []

    def add_observation(self, result: Dict[str, Any], action_block_num: int) -> None:
        self.observations.append(result)


def _runtime_stub(vault=None) -> ShadowRuntime:
    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.vault = vault or SimpleNamespace()
    rt.config = SimpleNamespace()
    return rt


# ─────────────────────────────────────────────────────────────────────────────
# COMPUTE
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_grant_bumps_iter_budget_and_observes():
    rt = _runtime_stub()
    ctx = _FakeContext()
    mat = {"materialised": True, "compute_grant": {"extra_iterations": 5}}

    new_budget = rt._apply_materialised_grant(
        mat, context=ctx, tools=[], tool_index=[], current_ab=1, iter_budget=10,
    )

    assert new_budget == 15
    assert len(ctx.observations) == 1
    obs = ctx.observations[0]
    assert obs["type"] == "harness_granted"
    assert "+5 iteration" in obs["message"]


def test_compute_grant_clamps_to_100():
    rt = _runtime_stub()
    ctx = _FakeContext()
    mat = {"materialised": True, "compute_grant": {"extra_iterations": 999}}

    new_budget = rt._apply_materialised_grant(
        mat, context=ctx, tools=[], tool_index=[], current_ab=1, iter_budget=10,
    )
    # Clamped to +100 (the inline block clamps extra_iterations to 0..100).
    assert new_budget == 110


# ─────────────────────────────────────────────────────────────────────────────
# TOOL
# ─────────────────────────────────────────────────────────────────────────────

def test_tool_grant_deploys_and_appends(monkeypatch):
    # Tool starts not-enabled → deploy is invoked → re-read returns enabled tool.
    not_enabled = SimpleNamespace(
        id="tool_x", name="my_tool", description="does x",
        parameter_names=["a"], parameters_schema={"a": {"type": "string"}},
        enabled=False,
    )
    enabled = SimpleNamespace(
        id="tool_x", name="my_tool", description="does x",
        parameter_names=["a"], parameters_schema={"a": {"type": "string"}},
        enabled=True,
    )

    get_tool_calls: List[str] = []

    def _get_tool(tid):
        get_tool_calls.append(tid)
        # First resolve (during materialise-resolve loop) → not_enabled;
        # after a successful deploy the block re-reads → enabled.
        return enabled if len(get_tool_calls) > 1 else not_enabled

    vault = SimpleNamespace(
        get_tool=_get_tool,
        find_tool_by_name=lambda n: None,
    )
    rt = _runtime_stub(vault)
    ctx = _FakeContext()

    monkeypatch.setattr(
        "systemu.pipelines.tool_deploy.deploy_forged_tool",
        lambda tool_id, vault, config: {"deployed": True},
    )

    tools: List[Any] = []
    tool_index: List[Dict[str, Any]] = []
    mat = {"materialised": True, "tool": "tool_x"}

    new_budget = rt._apply_materialised_grant(
        mat, context=ctx, tools=tools, tool_index=tool_index, current_ab=2, iter_budget=20,
    )

    # Budget unchanged for a TOOL grant.
    assert new_budget == 20
    # Tool appended to the live tool list + tool_index.
    assert len(tools) == 1 and tools[0].id == "tool_x"
    assert len(tool_index) == 1
    assert tool_index[0]["id"] == "tool_x"
    assert tool_index[0]["name"] == "my_tool"
    # A "ready to call" observation landed.
    assert any(o.get("type") == "harness_granted" for o in ctx.observations)


def test_tool_grant_failed_dryrun_surfaces_reason(monkeypatch):
    # v0.9.34.3: when the forged tool's automatic dry-run FAILS, the agent must
    # be handed the dry-run error (so it can repair its schema on a re-request),
    # not the old detail-free "pending" message.
    not_enabled = SimpleNamespace(
        id="tool_z", name="zlib_tool", description="compress",
        parameter_names=["input_path"],
        parameters_schema={"input_path": {"type": "string"},
                           "output_path": {"type": "string"}},
        enabled=False,
    )
    vault = SimpleNamespace(
        get_tool=lambda tid: not_enabled,   # stays not-enabled (deploy failed)
        find_tool_by_name=lambda n: None,
    )
    rt = _runtime_stub(vault)
    ctx = _FakeContext()

    err = "run() got an unexpected keyword argument 'output_path'"
    monkeypatch.setattr(
        "systemu.pipelines.tool_deploy.deploy_forged_tool",
        lambda tool_id, vault, config: {"deployed": False, "reason": err},
    )

    tools: List[Any] = []
    tool_index: List[Dict[str, Any]] = []
    mat = {"materialised": True, "tool": "tool_z"}
    new_budget = rt._apply_materialised_grant(
        mat, context=ctx, tools=tools, tool_index=tool_index, current_ab=2, iter_budget=20,
    )

    # Not callable: nothing appended to the live tool list; budget unchanged.
    assert new_budget == 20
    assert tools == [] and tool_index == []
    # The pending observation now carries the dry-run error + the repair hint.
    pend = next(o for o in ctx.observations
                if o.get("type") == "harness_granted_pending")
    msg = pend["message"]
    assert "FAILED its automatic dry-run" in msg
    assert err in msg                  # the actual dry-run error is surfaced
    assert "parameters_schema" in msg  # generic tool-authoring repair hint


# ─────────────────────────────────────────────────────────────────────────────
# Failure fallback
# ─────────────────────────────────────────────────────────────────────────────

def test_failed_materialisation_observes_grant_failed():
    rt = _runtime_stub()
    ctx = _FakeContext()
    mat = {"materialised": False, "reason": "forge failed"}

    new_budget = rt._apply_materialised_grant(
        mat, context=ctx, tools=[], tool_index=[], current_ab=3, iter_budget=7,
    )

    assert new_budget == 7
    assert len(ctx.observations) == 1
    assert ctx.observations[0]["type"] == "harness_grant_failed"
    assert "forge failed" in ctx.observations[0]["message"]
