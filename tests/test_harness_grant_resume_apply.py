"""Harness grant-resume — Task 4b: resume peels __HARNESS_GRANT__ and replays it.

When a parked run is re-queued via resume_after_grant, the snapshot carries a
``__HARNESS_GRANT__::<exec>::<json>`` note (the operator's resolved grant) and the
original ``__HARNESS_PENDING__`` note. At resume-start the loop peels both, then
``_apply_harness_grant`` consumes the grant:

  * DENY    → a harness_grant_failed observation (with the original fallback);
              the run proceeds (does NOT re-arbitrate, does NOT re-escalate).
  * INPUT   → injects the operator_answer as an observation.
  * COMPUTE → bumps the iteration budget via the shared _apply_materialised_grant.
  * TOOL    → deploys + registers the granted tool via the shared helper.

These reuse the SAME _apply_materialised_grant the autonomous GRANT path uses, so
resume is byte-identical to an auto-grant.

Driver: seed a snapshot, drive execute(resume_from_execution_id=...), assert the
effect landed, then terminate the loop fast (FAIL) without invoking the network.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import patch

from systemu.core.models import (
    Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType,
    Scroll, Objective,
)
from systemu.vault.vault import Vault
from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_vault(tmp_path):
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    return Vault(str(tmp_path))


@pytest.fixture
def mock_config(tmp_path):
    from sharing_on.config import Config
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    return cfg


@pytest.fixture
def runtime_setup(tmp_vault):
    shadow = Shadow(id="shadow_r", name="Resume Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)
    tool = Tool(id="tool_r", name="seed_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/seed_tool.py")
    tmp_vault.save_tool(tool)
    scroll = Scroll(id="scroll_r", name="Resume Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="Use capability",
                                          success_criteria="Done")])
    tmp_vault.save_scroll(scroll)
    activity = Activity(id="act_r", name="Resume Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_r"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    tmp_vault.save_activity(activity)
    return shadow, activity, scroll, tool


def _redirect_snapshot(monkeypatch, tmp_path):
    data_dir = tmp_path / "snap_data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot",
                        lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot",
                        lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot",
                        lambda eid, **kw: rd(eid, data_dir=data_dir))
    return data_dir


def _seed_grant_snapshot(data_dir, exec_id, *, grant_payload, pending=None):
    notes = []
    if pending is not None:
        notes.append(f"__HARNESS_PENDING__::{exec_id}::" + json.dumps(pending))
    notes.append(f"__HARNESS_GRANT__::{exec_id}::" + json.dumps(grant_payload))
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id="shadow_r", scroll_id="scroll_r",
        activity_id="act_r", iteration=2, completed_objective_ids=[],
        sticky_notes=notes,
    )
    write_snapshot(snap, data_dir=data_dir)


class _AssertNoArbitrate:
    """Governor whose arbitrate/materialise must NEVER be called on resume."""
    def __init__(self, *a, **kw):
        pass

    def arbitrate(self, *a, **kw):  # pragma: no cover
        raise AssertionError("resume must NOT re-arbitrate")

    def materialise(self, *a, **kw):  # pragma: no cover
        raise AssertionError("resume must NOT re-materialise via Governor")

    def revoke_leases(self, *a, **kw):
        pass


def _capture_observations(runtime):
    """Wrap _apply_harness_grant-adjacent observations by spying on context."""
    # We assert on observations via the spy installed in each test instead.


# ─────────────────────────────────────────────────────────────────────────────
# COMPUTE grant on resume → budget bump + observation, run continues
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_compute_grant_bumps_budget(tmp_vault, mock_config,
                                                 runtime_setup, tmp_path,
                                                 monkeypatch):
    shadow, activity, scroll, tool = runtime_setup
    data_dir = _redirect_snapshot(monkeypatch, tmp_path)
    exec_id = "exec_C1"
    _seed_grant_snapshot(
        data_dir, exec_id,
        grant_payload={"kind": "compute",
                       "compute_grant": {"extra_iterations": 7}},
        pending={"request_id": "h1", "kind": "compute", "spec": {},
                 "fallback": "do less"},
    )

    # The resumed run just FAILs immediately so we don't hit the network — the
    # grant is applied at resume-start BEFORE the first decision.
    decisions = [{"action": "FAIL", "reason": "done checking the grant"}]

    seen = {"compute": False}
    from systemu.runtime.context_builder import ExecutionContext
    real_add = ExecutionContext.add_observation

    def _spy_add(self, obs, ab=None):
        if isinstance(obs, dict) and "iteration" in str(obs.get("message", "")).lower():
            seen["compute"] = True
        return real_add(self, obs, ab)

    monkeypatch.setattr(ExecutionContext, "add_observation", _spy_add)

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _AssertNoArbitrate), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity,
                                       resume_from_execution_id=exec_id)

    # Run proceeded (did not park / re-escalate).
    assert result["status"] != "suspended_harness_escalation"
    # COMPUTE observation landed.
    assert seen["compute"], "expected a compute-grant (+iterations) observation"


# ─────────────────────────────────────────────────────────────────────────────
# TOOL grant on resume → deploy_forged_tool replayed, tool registered
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_tool_grant_registers_tool(tmp_vault, mock_config,
                                                runtime_setup, tmp_path,
                                                monkeypatch):
    shadow, activity, scroll, tool = runtime_setup
    data_dir = _redirect_snapshot(monkeypatch, tmp_path)
    exec_id = "exec_T1"

    # Pre-create the granted tool in the vault (forged earlier by the reconciler),
    # not yet enabled — the resume should deploy + enable + register it.
    granted = Tool(id="tool_geo", name="geocode_place", description="geocoder",
                   tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.FORGED,
                   enabled=False,
                   implementation_path="vault/tools/implementations/geocode_place.py")
    tmp_vault.save_tool(granted)

    _seed_grant_snapshot(
        data_dir, exec_id,
        grant_payload={"kind": "tool", "granted": True,
                       "granted_tool": "geocode_place", "tool_id": "tool_geo",
                       "lease_id": "lease_x"},
        pending={"request_id": "h2", "kind": "tool",
                 "spec": {"name": "geocode_place"}, "fallback": "guess"},
    )

    decisions = [{"action": "FAIL", "reason": "done checking the grant"}]

    deploy_calls = {"n": 0}

    def _fake_deploy(tool_id, vault, config):
        deploy_calls["n"] += 1
        # mark the tool enabled, mirroring a real deploy
        t = vault.get_tool(tool_id)
        t.enabled = True
        t.status = ToolStatus.DEPLOYED
        vault.save_tool(t)
        return {"deployed": True}

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _AssertNoArbitrate), \
         patch("systemu.pipelines.tool_deploy.deploy_forged_tool", _fake_deploy), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity,
                                       resume_from_execution_id=exec_id)

    assert result["status"] != "suspended_harness_escalation"
    assert deploy_calls["n"] == 1, "resume must replay deploy_forged_tool for TOOL grant"


# ─────────────────────────────────────────────────────────────────────────────
# DENY grant on resume → fallback observation, run proceeds (no re-escalate)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_deny_grant_adds_fallback_observation(tmp_vault, mock_config,
                                                           runtime_setup, tmp_path,
                                                           monkeypatch):
    shadow, activity, scroll, tool = runtime_setup
    data_dir = _redirect_snapshot(monkeypatch, tmp_path)
    exec_id = "exec_D1"
    _seed_grant_snapshot(
        data_dir, exec_id,
        grant_payload={"kind": "tool", "denied": True,
                       "rationale": "policy forbids forging"},
        pending={"request_id": "h3", "kind": "tool",
                 "spec": {"name": "geocode_place"},
                 "fallback": "use the cached coordinates"},
    )

    decisions = [{"action": "FAIL", "reason": "fell back"}]

    captured = {"obs": []}
    from systemu.runtime.context_builder import ExecutionContext
    real_add = ExecutionContext.add_observation

    def _spy_add(self, obs, ab=None):
        if isinstance(obs, dict):
            captured["obs"].append(obs)
        return real_add(self, obs, ab)

    monkeypatch.setattr(ExecutionContext, "add_observation", _spy_add)

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _AssertNoArbitrate), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity,
                                       resume_from_execution_id=exec_id)

    assert result["status"] != "suspended_harness_escalation"
    # A failure/fallback observation carrying the original fallback landed.
    fail_obs = [o for o in captured["obs"]
                if "fail" in str(o.get("type", "")).lower()
                or "cached coordinates" in str(o.get("message", ""))]
    assert fail_obs, f"expected a deny/fallback observation, got {captured['obs']}"
    assert any("cached coordinates" in str(o.get("message", "")) for o in fail_obs)


# ─────────────────────────────────────────────────────────────────────────────
# INPUT grant on resume → operator_answer injected as an observation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_input_grant_injects_answer(tmp_vault, mock_config,
                                                 runtime_setup, tmp_path,
                                                 monkeypatch):
    shadow, activity, scroll, tool = runtime_setup
    data_dir = _redirect_snapshot(monkeypatch, tmp_path)
    exec_id = "exec_I1"
    _seed_grant_snapshot(
        data_dir, exec_id,
        grant_payload={"kind": "INPUT",
                       "operator_answer": "Use the Bangalore office address"},
        pending={"request_id": "h4", "kind": "input",
                 "spec": {"question": "which office?"}, "fallback": ""},
    )

    decisions = [{"action": "FAIL", "reason": "used the answer"}]

    captured = {"obs": []}
    from systemu.runtime.context_builder import ExecutionContext
    real_add = ExecutionContext.add_observation

    def _spy_add(self, obs, ab=None):
        if isinstance(obs, dict):
            captured["obs"].append(obs)
        return real_add(self, obs, ab)

    monkeypatch.setattr(ExecutionContext, "add_observation", _spy_add)

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _AssertNoArbitrate), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity,
                                       resume_from_execution_id=exec_id)

    assert result["status"] != "suspended_harness_escalation"
    assert any("Bangalore" in str(o.get("message", "")) for o in captured["obs"]), \
        f"expected the operator_answer injected, got {captured['obs']}"
