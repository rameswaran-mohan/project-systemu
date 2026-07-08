"""S3 / R-A7 wave-3a — RESUME: no silent re-submit of an external effect.

Two invariants:

  (1) A resume whose external objective has a STORED ``confirmed=True`` bit
      re-credits it WITHOUT re-fetching / re-verifying — the ExternalVerifier is
      NOT re-invoked (a spy asserts 0 calls). (This extends the S4 resume
      short-circuit test — S3 adds the guarantee that the verifier machinery is
      not even touched.)

  (2) A resume whose external objective is UNCONFIRMED must NOT silently
      re-execute the effectful (submit) path — a blind re-submit is a
      double-submit hazard. Instead the runtime surfaces an OPERATOR CARD (the
      existing InboxQueue rail) and parks; it does not call the effectful tool
      again on its own.

Modelled on tests/test_s4_failclosed_resume.py (the resume recredit harness).
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import patch


# ─────────────────────────────────────────────────────────────────────────────
#  Harness
# ─────────────────────────────────────────────────────────────────────────────

def _build_entities_objs(tmp_path, objectives):
    from systemu.vault.vault import Vault
    from systemu.core.models import (
        Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType, Scroll,
    )
    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ["scrolls", "activities", "shadow_army", "skills",
                    "tools", "evolutions", "decisions"]:
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    vault = Vault(str(tmp_path))

    shadow = Shadow(id="shadow_s3r", name="S3R Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_s3r", name="api_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/api_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_s3r", name="S3R Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=objectives)
    vault.save_scroll(scroll)
    activity = Activity(id="act_s3r", name="S3R Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_s3r"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


def _redirect_snapshot_io(monkeypatch, data_dir):
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot", lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot", lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot", lambda eid, **kw: rd(eid, data_dir=data_dir))


def _mk_snapshot(shadow, activity, *, exec_id, graph, completed=None,
                 external_evidence=None):
    from systemu.runtime.execution_snapshot import ExecutionSnapshot
    return ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_s3r",
        activity_id=activity.id, iteration=1,
        completed_objective_ids=list(completed or []),
        objective_graph=graph, next_objective_id=99,
        external_evidence=dict(external_evidence or {}),
    )


def _external_obj(**overrides):
    from systemu.core.models import Objective
    base = dict(id=1, goal="POST the row to the external API",
                success_criteria="row visible via readback",
                requires_external_verification=True)
    base.update(overrides)
    return Objective(**base)


# ─────────────────────────────────────────────────────────────────────────────
#  (1) stored confirmed ⇒ re-credit with NO re-verify (verifier spy 0 calls)
# ─────────────────────────────────────────────────────────────────────────────

class _CaptureAbort(Exception):
    pass


def test_resume_confirmed_no_reverify_verifier_zero_calls(tmp_path, monkeypatch):
    """(1) a resumed run with a stored confirmed=True external bit credits with NO
    re-fetch / NO re-verify — the ExternalVerifier.verify is NOT invoked at all
    (a spy asserts 0 calls). The stored bit short-circuits the resume recredit."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    import systemu.runtime.external_verifier as _ev
    from systemu.core.models import Objective

    graph = [
        _external_obj(id=1, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]
    objectives = [
        _external_obj(id=1, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]

    vault, shadow, activity = _build_entities_objs(tmp_path, objectives)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # Spy the verifier: any call means a re-verify happened (bad).
    verify_calls = {"n": 0}
    _orig_verify = _ev.ExternalVerifier.verify

    def _spy_verify(self, *a, **k):
        verify_calls["n"] += 1
        return _orig_verify(self, *a, **k)
    monkeypatch.setattr(_ev.ExternalVerifier, "verify", _spy_verify)

    # Capture the credited set right after the resume recredit loop, then unwind.
    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy_resolve(**kw):
        import inspect
        try:
            frame = inspect.currentframe().f_back
            _comp = frame.f_locals.get("completed_objectives")
            if _comp is not None:
                captured["completed"] = set(_comp)
        except Exception:
            pass
        raise _CaptureAbort()
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy_resolve)

    runtime = ShadowRuntime(cfg, vault)

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        from systemu.runtime.tool_sandbox import ToolResult
        return ToolResult(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    snap = _mk_snapshot(shadow, activity, exec_id="exec_s3r_confirmed",
                        graph=graph, completed=[2],
                        external_evidence={"1": {"objective_id": 1, "confirmed": True}})
    from systemu.runtime.execution_snapshot import write_snapshot
    write_snapshot(snap, data_dir=data_dir)

    decisions = [{"action": "FAIL", "reason": "unreachable — capture unwinds first"}]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        try:
            asyncio.run(runtime.execute(
                shadow, activity, resume_from_execution_id=snap.execution_id))
        except _CaptureAbort:
            pass

    assert 1 in captured.get("completed", set()), (
        "an external objective WITH a stored confirmed bit must be re-credited on "
        f"resume; completed={captured.get('completed')}")
    assert verify_calls["n"] == 0, (
        "the ExternalVerifier must NOT be re-invoked for a stored-confirmed "
        f"external objective (no re-verify); verify was called {verify_calls['n']}x")


# ─────────────────────────────────────────────────────────────────────────────
#  (2) unconfirmed external on resume ⇒ NO silent re-submit — operator card / park
# ─────────────────────────────────────────────────────────────────────────────

def test_resume_unconfirmed_does_not_silently_resubmit(tmp_path, monkeypatch):
    """(2) a resumed run whose external objective is UNCONFIRMED must NOT silently
    re-execute the effectful (submit) tool call — it surfaces an operator card via
    the InboxQueue rail and parks. We spy the effectful tool: it must NOT be
    invoked for the external objective on its own, and an operator card must be
    enqueued (or the run parks in a suspended state)."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    from systemu.core.models import Objective

    graph = [_external_obj(id=1, depends_on=[])]
    objectives = [_external_obj(id=1, depends_on=[])]

    vault, shadow, activity = _build_entities_objs(tmp_path, objectives)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # Spy the operator-card rail (InboxQueue.enqueue) — the guard must use it.
    enqueued = []
    import systemu.interface.command.inbox as _inbox
    _orig_enqueue = _inbox.InboxQueue.enqueue

    def _spy_enqueue(self, descriptor, *a, **k):
        try:
            enqueued.append(getattr(descriptor, "title", str(descriptor)))
        except Exception:
            enqueued.append("card")
        return "decision_fake"
    monkeypatch.setattr(_inbox.InboxQueue, "enqueue", _spy_enqueue)

    runtime = ShadowRuntime(cfg, vault)

    # Spy the effectful tool: a call here for the external objective is a SILENT
    # re-submit (the failure mode this test guards against).
    tool_calls = {"n": 0}

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        tool_calls["n"] += 1
        from systemu.runtime.tool_sandbox import ToolResult
        return ToolResult(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    # NO confirmed evidence for obj 1 → unconfirmed on resume.
    snap = _mk_snapshot(shadow, activity, exec_id="exec_s3r_unconfirmed",
                        graph=graph, completed=[], external_evidence={})
    from systemu.runtime.execution_snapshot import write_snapshot
    write_snapshot(snap, data_dir=data_dir)

    # The LLM (if reached) would immediately re-issue the submit — the guard must
    # prevent that from running silently.
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "re-submit the external effect"},
        {"action": "FAIL", "reason": "terminal"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=snap.execution_id))

    # The run must NOT have silently re-submitted the effectful call AND must have
    # surfaced an operator card (or parked in a suspended state).
    parked = str(result.get("status", "")).startswith("suspended")
    assert enqueued or parked, (
        "an unconfirmed external objective on resume must surface an operator card "
        f"or park — neither happened; status={result.get('status')} cards={enqueued}")
    assert tool_calls["n"] == 0, (
        "the effectful submit tool must NOT be re-invoked silently on resume for an "
        f"unconfirmed external objective; it was called {tool_calls['n']}x")
    assert result.get("status") != "success", (
        "an unconfirmed external objective must not finalize success on resume; "
        f"got {result.get('status')}")
