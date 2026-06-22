"""Harness grant-resume — Task 4a: blocking ESCALATE must snapshot + park.

When the Governor ESCALATEs a *blocking* harness request, the loop must:
  (i)   write an ExecutionSnapshot for the execution_id carrying a
        ``__HARNESS_PENDING__`` note (kind + spec), so the daemon reconciler /
        resume_after_grant can pick it up;
  (ii)  return a result with status == "suspended_harness_escalation" (the
        Supervisor parks, never retries — Task 1);
  (iii) NOT complete the activity.

A *non-blocking* ESCALATE keeps the OLD behaviour: surface the card + add a
proceed-with-fallback observation; NO snapshot; the loop continues.

Driver mirrors tests/test_shadow_runtime.py (patch llm_call_json with a list of
decisions; patch the Governor class so its arbitrate() returns ESCALATE).
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from systemu.core.models import (
    Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType,
    Scroll, Objective, HarnessVerdict, HarnessDecision, RiskBand,
)
from systemu.vault.vault import Vault


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (mirror test_shadow_runtime.py)
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
    shadow = Shadow(id="shadow_h", name="Harness Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)
    tool = Tool(id="tool_h", name="seed_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/seed_tool.py")
    tmp_vault.save_tool(tool)
    scroll = Scroll(id="scroll_h", name="Harness Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="Need a capability",
                                          success_criteria="Done")])
    tmp_vault.save_scroll(scroll)
    activity = Activity(id="act_h", name="Harness Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_h"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    tmp_vault.save_activity(activity)
    return shadow, activity, scroll, tool


class _EscalateGovernor:
    """A Governor stub whose arbitrate() always ESCALATEs."""
    def __init__(self, *a, **kw):
        pass

    def arbitrate(self, req, context=None):  # v0.9.33 Bug 2/3: the arbiter now
        # receives a per-run arbitration context (requests_this_run + nesting
        # depth). The stub ignores it but MUST accept it — otherwise the loop's
        # ``_gov.arbitrate(_req, context=...)`` raises TypeError, which the
        # REQUEST_HARNESS try/except swallows, so the run never parks: it falls
        # through to the next decision and hangs in the network goal-verifier.
        return HarnessVerdict(
            request_id=req.request_id,
            decision=HarnessDecision.ESCALATE,
            risk_band=RiskBand.HIGH,
            rationale="needs operator approval",
        )

    def materialise(self, *a, **kw):  # pragma: no cover - should not be hit
        raise AssertionError("materialise must NOT be called on ESCALATE")

    def revoke_leases(self, *a, **kw):
        pass


def _data_dir(monkeypatch, tmp_path):
    """Redirect the snapshot store to a tmp data dir so we don't touch ./data."""
    data_dir = tmp_path / "snap_data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    real_write = _es.write_snapshot
    real_read = _es.read_snapshot
    real_del = _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot",
                        lambda snap, **kw: real_write(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot",
                        lambda eid, **kw: real_read(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot",
                        lambda eid, **kw: real_del(eid, data_dir=data_dir))
    return data_dir, real_read


# ─────────────────────────────────────────────────────────────────────────────
# 4a — blocking ESCALATE → snapshot + park + suspend-return
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_blocking_escalate_snapshots_and_parks(tmp_vault, mock_config,
                                                     runtime_setup, tmp_path,
                                                     monkeypatch):
    shadow, activity, scroll, tool = runtime_setup
    data_dir, real_read = _data_dir(monkeypatch, tmp_path)

    # The agent requests a TOOL capability with blocking=True.
    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "tool",
         "spec": {"name": "geocode_place", "purpose": "resolve a place"},
         "rationale": "no geocoder available", "fallback": "guess coords",
         "blocking": True},
        # A second decision the loop should NEVER reach (it parks above).
        {"action": "COMPLETE", "summary": "should not happen"},
    ]

    surfaced = {"n": 0}

    def _fake_surface(req, verdict, **kw):
        surfaced["n"] += 1
        # coords must be threaded for the reconciler
        assert kw.get("activity_id") == activity.id
        assert kw.get("shadow_id") == shadow.id
        return "decision_x"

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _EscalateGovernor), \
         patch("systemu.interface.harness_review.surface_harness_request", _fake_surface), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity)

    # (ii) suspend-return status
    assert result["status"] == "suspended_harness_escalation"
    # carries resume coords
    assert result["activity_id"] == activity.id
    assert result["shadow_id"] == shadow.id
    exec_id = result["execution_id"]
    assert exec_id

    # the operator card was surfaced
    assert surfaced["n"] == 1

    # (i) a snapshot exists with a __HARNESS_PENDING__ note carrying kind+spec
    snap = real_read(exec_id, data_dir=data_dir)
    assert snap is not None, "blocking ESCALATE must write a resume snapshot"
    pending = [n for n in snap.sticky_notes
               if n.startswith(f"__HARNESS_PENDING__::{exec_id}::")]
    assert len(pending) == 1
    import json
    payload = json.loads(pending[0].split("::", 2)[2])
    assert payload["kind"] == "tool"
    assert payload["spec"]["name"] == "geocode_place"
    assert payload["fallback"] == "guess coords"

    # (iii) activity NOT completed — execution log has no success row
    updated = tmp_vault.get_shadow(shadow.id)
    statuses = [e.get("status") for e in (updated.execution_log or [])]
    assert "success" not in statuses


# ─────────────────────────────────────────────────────────────────────────────
# 4a — non-blocking ESCALATE → OLD proceed-with-fallback (no snapshot)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nonblocking_escalate_proceeds_no_snapshot(tmp_vault, mock_config,
                                                         runtime_setup, tmp_path,
                                                         monkeypatch):
    shadow, activity, scroll, tool = runtime_setup
    data_dir, real_read = _data_dir(monkeypatch, tmp_path)

    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "tool",
         "spec": {"name": "geocode_place"},
         "rationale": "no geocoder", "fallback": "guess coords",
         "blocking": False},
        # The loop CONTINUES past the non-blocking escalate and reaches the next
        # decision. We FAIL here (rather than COMPLETE) purely to terminate fast
        # without invoking the network goal-verifier — the point is the loop did
        # NOT park on the escalate.
        {"action": "FAIL", "reason": "fell back, then gave up"},
    ]

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _EscalateGovernor), \
         patch("systemu.interface.harness_review.surface_harness_request",
               lambda *a, **k: "decision_y"), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity)

    # OLD behaviour: the run did NOT park on a suspend status.
    assert result["status"] != "suspended_harness_escalation"
    # No snapshot was written for this execution.
    snap = real_read(result["execution_id"], data_dir=data_dir)
    assert snap is None, "non-blocking ESCALATE must NOT snapshot/park"
