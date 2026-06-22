"""v0.9.36 Bug 9 + Bug 10 — terminal harness finalize + request instrumentation.

Bug 9: the terminal closure ``_revoke_harness_leases()`` (request-outcome
reconciliation + lease-revoke + MCP unregister) used to fire only on the
COMPLETE / FAIL / escalate-suspend / goal-verified paths, so the partial /
max-iterations / exception exits — and the suspend→approve→resume completions
that dominate the HIGH-risk families — never finalized (174 lease-mints /
3 lease-revokes in the scored 180-trial run). Two symptoms, one root cause:

  * Symptom A — the namespaced MCP tools a run registers leaked into the NEXT
    run's catalog. The v2 tool registry is a PROCESS-GLOBAL singleton; the
    unregister lives on the lease-revoke path, and a resumed run mints its lease
    under the now-dead pre-suspend Governor, so the lease-keyed revoke finds
    nothing and never unregisters.
  * Symptom B — the request-outcome reconciliation (RQ1 instrumentation) was
    never written for those exits.

Fix: GUARANTEE the finalize once per run in execute()'s finally block
(idempotent), and as defense-in-depth unregister every MCP server the run
registered on a genuine TERMINAL exit (``record_run=True``) — never on a suspend
(``record_run=False``), which resumes and still needs the tools.

Bug 10: ``attempts_before`` / ``confidence`` are read off the decision JSON but
the prompt never documents them, so they are always null. Generic prompt fix.

Driver mirrors tests/test_harness_grant_resume_apply.py.
"""
from __future__ import annotations

import json
import pathlib
import pytest
from unittest.mock import patch

from systemu.core.models import (
    Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType,
    Scroll, Objective, HarnessVerdict, HarnessDecision, RiskBand,
)
from systemu.vault.vault import Vault
from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (mirror tests/test_harness_grant_resume_apply.py)
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
    shadow = Shadow(id="shadow_b9", name="Bug9 Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    tmp_vault.save_shadow(shadow)
    tool = Tool(id="tool_b9", name="seed_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/seed_tool.py")
    tmp_vault.save_tool(tool)
    scroll = Scroll(id="scroll_b9", name="Bug9 Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="Need a capability",
                                          success_criteria="Done")])
    tmp_vault.save_scroll(scroll)
    activity = Activity(id="act_b9", name="Bug9 Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_b9"], required_skill_ids=[],
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
        execution_id=exec_id, shadow_id="shadow_b9", scroll_id="scroll_b9",
        activity_id="act_b9", iteration=2, completed_objective_ids=[],
        sticky_notes=notes,
    )
    write_snapshot(snap, data_dir=data_dir)


def _mcp_grant_payload():
    return {
        "kind": "mcp", "granted": True, "lease_id": "lease_mcp",
        "mcp": {"server_id": "lookup", "label": "Lookup", "transport": "stdio",
                "tools": [{"name": "resolve", "description": "resolve a code",
                           "parameters_schema": {"type": "object"},
                           "annotations": {"readOnlyHint": True}}]},
    }


def _patch_registry(monkeypatch):
    """Spy register/unregister on the registry_bridge module (the register site
    + the terminal unregister both lazy-import from here)."""
    import systemu.runtime.mcp.sdk.registry_bridge as rb_mod
    calls = {"register": [], "unregister": []}

    def _fake_register(vault, server, tools):
        calls["register"].append(server)
        return [f"mcp__{server}__resolve"]

    def _fake_unregister(server):
        calls["unregister"].append(server)
        return 1

    monkeypatch.setattr(rb_mod, "register_server_tools", _fake_register, raising=False)
    monkeypatch.setattr(rb_mod, "unregister_server_tools", _fake_unregister, raising=False)
    return calls


# ─────────────────────────────────────────────────────────────────────────────
# Bug 9 Symptom B — finalize fires on an exit no explicit call site covers
# ─────────────────────────────────────────────────────────────────────────────

class _SpyGovernor:
    revoked: list = []
    reconciled: list = []

    def __init__(self, *a, **k):
        pass

    def arbitrate(self, *a, **k):  # pragma: no cover - not reached here
        raise AssertionError("arbitrate not expected in this test")

    def materialise(self, *a, **k):  # pragma: no cover
        raise AssertionError("materialise not expected in this test")

    def revoke_leases(self, execution_id):
        type(self).revoked.append(execution_id)
        return 0

    def write_outcome_reconciliation(self, execution_id, used, *,
                                     run_success=True, vault=None):
        type(self).reconciled.append((execution_id, run_success))
        return 0


@pytest.mark.asyncio
async def test_finally_finalizes_on_exception_exit(tmp_vault, mock_config,
                                                   runtime_setup, tmp_path,
                                                   monkeypatch):
    """A crash mid-loop has NO explicit finalize call site; only the finally
    block guarantees the terminal reconciliation + lease-revoke still run."""
    shadow, activity, scroll, tool = runtime_setup
    _redirect_snapshot(monkeypatch, tmp_path)
    _SpyGovernor.revoked = []
    _SpyGovernor.reconciled = []

    import systemu.runtime.shadow_runtime as sr
    monkeypatch.setattr(sr, "MAX_ITERATIONS", 3, raising=False)

    def _boom(*a, **k):
        raise RuntimeError("llm crashed mid-loop")

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=_boom), \
         patch("systemu.runtime.governor.Governor", _SpyGovernor), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        try:
            await runtime.execute(shadow, activity)
        except Exception:
            pass  # propagation is fine — the finally must still finalize

    assert _SpyGovernor.revoked, \
        "finally did not revoke leases on the uncovered (exception) terminal exit"
    assert _SpyGovernor.reconciled, \
        "finally did not write the request-outcome reconciliation (Symptom B)"
    # an uncovered fall-through exit is a non-success terminal
    assert _SpyGovernor.reconciled[-1][1] is False


# ─────────────────────────────────────────────────────────────────────────────
# Bug 9 Symptom A — the resume-completion MCP leak (lease-keyed revoke can't reach)
# ─────────────────────────────────────────────────────────────────────────────

class _NoArbitrateNoRevoke:
    """Resume governor: never arbitrates; revoke_leases is a NO-OP — mirrors the
    resume-completion case where the lease lives in the dead pre-suspend Governor
    so the lease-keyed unregister can't reach the freshly-registered server."""
    def __init__(self, *a, **k):
        pass

    def arbitrate(self, *a, **k):  # pragma: no cover
        raise AssertionError("resume must not re-arbitrate")

    def materialise(self, *a, **k):  # pragma: no cover
        raise AssertionError("resume must not re-materialise")

    def revoke_leases(self, *a, **k):
        return 0  # no-op: this run's Governor holds no lease for the server

    def write_outcome_reconciliation(self, *a, **k):
        return 0


@pytest.mark.asyncio
async def test_resume_mcp_unregistered_at_terminal_despite_noop_revoke(
        tmp_vault, mock_config, runtime_setup, tmp_path, monkeypatch):
    """Resume registers an MCP server, then FAILs (a genuine TERMINAL exit). The
    lease-keyed revoke is a no-op (the lease is in the dead pre-suspend Governor),
    so ONLY the defense-in-depth tracked-unregister can close the cross-run leak."""
    shadow, activity, scroll, tool = runtime_setup
    data_dir = _redirect_snapshot(monkeypatch, tmp_path)
    calls = _patch_registry(monkeypatch)
    exec_id = "exec_mcp_leak"
    _seed_grant_snapshot(
        data_dir, exec_id,
        grant_payload=_mcp_grant_payload(),
        pending={"request_id": "h_mcp", "kind": "mcp",
                 "spec": {"server_id": "lookup"}, "fallback": "ask operator"},
    )
    decisions = [{"action": "FAIL", "reason": "done, tear down"}]

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _NoArbitrateNoRevoke), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        await runtime.execute(shadow, activity, resume_from_execution_id=exec_id)

    assert "lookup" in calls["register"], "resume did not register the MCP server"
    assert "lookup" in calls["unregister"], (
        "terminal exit did not unregister the run's MCP server — cross-run leak")


def test_apply_materialised_grant_mcp_tracks_registered_server(monkeypatch):
    """Unit: the MCP grant-apply records the server on the runtime so the
    terminal finalize can tear it down. Robust to a __new__-built runtime."""
    import systemu.runtime.mcp.sdk.registry_bridge as rb_mod
    monkeypatch.setattr(rb_mod, "register_server_tools",
                        lambda vault, server, tools: [f"mcp__{server}__x"],
                        raising=False)
    from systemu.runtime.shadow_runtime import ShadowRuntime

    class _Ctx:
        def __init__(self):
            self.observations = []

        def add_observation(self, payload, ab):
            self.observations.append(payload)

    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt.vault = None
    rt.config = None
    mat = {"materialised": True, "lease_id": "L",
           "mcp": {"server_id": "lookup", "label": "Lookup", "transport": "stdio",
                   "tools": [{"name": "x", "description": "d",
                              "parameters_schema": {"type": "object"},
                              "annotations": {}}]}}
    rt._apply_materialised_grant(mat, context=_Ctx(), tools=[], tool_index=[],
                                 current_ab=0, iter_budget=5)
    assert getattr(rt, "_mcp_servers_registered_this_run", set()) == {"lookup"}


# ─────────────────────────────────────────────────────────────────────────────
# Bug 9 — suspend is NOT terminal: it must NOT tear down the run's MCP tools
# ─────────────────────────────────────────────────────────────────────────────

class _EscalateNoRevoke:
    def __init__(self, *a, **k):
        pass

    def arbitrate(self, req, context=None):
        return HarnessVerdict(request_id=req.request_id,
                              decision=HarnessDecision.ESCALATE,
                              risk_band=RiskBand.HIGH,
                              rationale="needs operator approval")

    def materialise(self, *a, **k):  # pragma: no cover
        raise AssertionError("escalate must not materialise")

    def revoke_leases(self, *a, **k):
        return 0

    def write_outcome_reconciliation(self, *a, **k):
        return 0


@pytest.mark.asyncio
async def test_suspend_after_mcp_registration_does_not_unregister(
        tmp_vault, mock_config, runtime_setup, tmp_path, monkeypatch):
    """Resume registers an MCP server, then a BLOCKING request ESCALATEs → the
    run SUSPENDS (parks for approval). A suspend is not terminal — the resumed
    run still needs the tools, so the terminal unregister MUST NOT fire."""
    shadow, activity, scroll, tool = runtime_setup
    data_dir = _redirect_snapshot(monkeypatch, tmp_path)
    calls = _patch_registry(monkeypatch)
    exec_id = "exec_mcp_suspend"
    _seed_grant_snapshot(
        data_dir, exec_id,
        grant_payload=_mcp_grant_payload(),
        pending={"request_id": "h_mcp", "kind": "mcp",
                 "spec": {"server_id": "lookup"}, "fallback": "ask operator"},
    )
    decisions = [
        {"action": "REQUEST_HARNESS", "kind": "tool",
         "spec": {"name": "x"}, "rationale": "need it", "fallback": "guess",
         "blocking": True},
        {"action": "COMPLETE", "summary": "unreached"},
    ]

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.governor.Governor", _EscalateNoRevoke), \
         patch("systemu.interface.harness_review.surface_harness_request",
               lambda *a, **k: "decision_z"), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(mock_config, tmp_vault)
        result = await runtime.execute(shadow, activity,
                                       resume_from_execution_id=exec_id)

    assert result["status"] == "suspended_harness_escalation"
    assert "lookup" in calls["register"], "resume did not register the MCP server"
    assert "lookup" not in calls["unregister"], (
        "suspend wrongly unregistered tools the resumed run still needs")


# ─────────────────────────────────────────────────────────────────────────────
# Bug 10 — the REQUEST_HARNESS prompt documents attempts_before + confidence
# ─────────────────────────────────────────────────────────────────────────────

def test_request_harness_prompt_documents_attempts_before_and_confidence():
    p = (pathlib.Path(__file__).resolve().parents[1]
         / "systemu" / "prompts" / "execute_step.md")
    text = p.read_text(encoding="utf-8")
    start = text.index("REQUEST_HARNESS")
    section = text[start: text.index("ASK_OPERATOR", start)]
    assert "attempts_before" in section, \
        "REQUEST_HARNESS prompt never documents attempts_before (Bug 10)"
    assert "confidence" in section, \
        "REQUEST_HARNESS prompt never documents confidence (Bug 10)"
