"""S4 (fail-closed external-effect credit) — WAVE 2, RESUME recredit site.

The invariant under test (shadow_runtime.py ~:3943-3965, the resume durable-
evidence recredit loop):

  On resume, an uncredited Objective with ``requires_external_verification=True``
  is re-credited ONLY on the STORED external bit — ``_read_external_ok(context,
  obj_id)`` True (a persisted ExternalEvidence.confirmed, re-seeded onto
  ``context._external_evidence`` by wave-1's ``apply_to_context``). It must NOT
  run the epoch-delta local verifier (``recredit_on_resume``) — that verifier
  soft-passes a verifier=None objective and would silently re-credit an unverified
  external effect. NO re-fetch / re-verify: the stored confirmed bit alone decides.
  Non-external objectives keep the existing ``recredit_on_resume`` path unchanged.
  The gate STACKS on the B9 ``blocked_ids`` filter (never bypasses it).

Failure-injection, test-first. Drives the REAL resume recredit loop via
execute(resume_from_execution_id=...), capturing the credited set at the exact
point after the loop runs (a resolve-spy that unwinds, mirroring
tests/test_ra10_runtime_fold.py::_capture_completed_at_resolve).
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

    shadow = Shadow(id="shadow_s4r", name="S4R Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_s4r", name="api_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/api_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_s4r", name="S4R Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=objectives)
    vault.save_scroll(scroll)
    activity = Activity(id="act_s4r", name="S4R Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_s4r"], required_skill_ids=[],
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


class _CaptureAbort(Exception):
    """Unwind execute() right after the resume recredit loop has run — everything
    the resume-gate tests assert on (the credited set) is final by then."""


def _capture_completed_at_resolve(monkeypatch):
    """Spy _resolve_objectives_for_run: snapshot the LIVE ``completed_objectives``
    local (the resume recredit loop is the last thing to mutate it before this
    call) and unwind. Mirrors test_ra10_runtime_fold.py::_capture_completed_at_resolve."""
    import inspect
    import systemu.runtime.shadow_runtime as _sr
    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        try:
            frame = inspect.currentframe().f_back
            _comp = frame.f_locals.get("completed_objectives")
            if _comp is not None:
                captured["completed"] = set(_comp)
        except Exception:
            pass
        captured["context"] = kw.get("context")
        raise _CaptureAbort()

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)
    return captured


def _run_resume_capture(tmp_path, monkeypatch, *, snap, objectives,
                        recredit_spy=None):
    """Drive execute(resume_from_execution_id=snap.execution_id) and return the
    captured ``{"completed": set, "context": ctx}`` snapshotted right after the
    resume recredit loop. ``recredit_spy`` (optional) replaces
    ``recredit_on_resume`` so a test can count its calls / force a soft-pass."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_entities_objs(tmp_path, objectives)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    from systemu.runtime.execution_snapshot import write_snapshot
    write_snapshot(snap, data_dir=data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    if recredit_spy is not None:
        monkeypatch.setattr(_sr, "recredit_on_resume", recredit_spy, raising=False)

    captured = _capture_completed_at_resolve(monkeypatch)

    runtime = ShadowRuntime(cfg, vault)

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        from systemu.runtime.tool_sandbox import ToolResult
        return ToolResult(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    decisions = [{"action": "FAIL", "reason": "unreachable — capture unwinds first"}]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        try:
            asyncio.run(runtime.execute(
                shadow, activity, resume_from_execution_id=snap.execution_id))
        except _CaptureAbort:
            pass
    return captured


def _mk_snapshot(shadow, activity, *, exec_id, graph, completed=None,
                 external_evidence=None):
    from systemu.runtime.execution_snapshot import ExecutionSnapshot
    return ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_s4r",
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


def _softpass_recredit(**kw):
    """A recredit_on_resume replacement modelling the epoch-delta verifier
    SOFT-PASSING (verifier=None → credited). If S4 calls this for an external
    objective the gate has leaked; the resume gate must SHORT-CIRCUIT before it."""
    from systemu.runtime.shadow_runtime import CompletionOutcome, ObjectiveState
    return CompletionOutcome(credited=True, state=ObjectiveState())


# ─────────────────────────────────────────────────────────────────────────────
#  Failure-injection tests (S4 gate, RESUME site)
# ─────────────────────────────────────────────────────────────────────────────

def test_resume_external_no_evidence_fails_closed(tmp_path, monkeypatch):
    """(1) resume with an external objective; the epoch-delta verifier WOULD
    soft-pass, but there is NO external_evidence → NOT re-credited."""
    # A DIFFERENT objective (id=2) is already credited so the recredit loop RUNS.
    from systemu.core.models import Objective
    graph = [
        _external_obj(id=1, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]
    objectives = [
        _external_obj(id=1, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]
    snap = _mk_snapshot(*_build_entities_objs(tmp_path, objectives)[1:],
                        exec_id="exec_s4r_noev", graph=graph, completed=[2],
                        external_evidence={})  # NO evidence for obj 1
    captured = _run_resume_capture(
        tmp_path, monkeypatch, snap=snap, objectives=objectives,
        recredit_spy=_softpass_recredit)
    assert 1 not in captured.get("completed", set()), (
        "an external objective with no stored evidence must NOT be re-credited on "
        f"resume; completed={captured.get('completed')}")


def test_resume_short_circuits_on_stored_confirmed_no_refetch(tmp_path, monkeypatch):
    """(2) resume with a stored confirmed external bit → re-credited AND the local
    epoch-delta verifier (recredit_on_resume) is NOT invoked for that objective
    (a spy asserts 0 calls) — the stored bit short-circuits, no re-fetch."""
    from systemu.core.models import Objective
    graph = [
        _external_obj(id=1, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]
    objectives = [
        _external_obj(id=1, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]

    calls = {"ids": []}

    def _spy_recredit(**kw):
        # Record which objective ids reach the local verifier.
        obj = kw.get("objective")
        calls["ids"].append(getattr(obj, "id", None))
        # A non-external objective (id=2 is already credited so it won't be here);
        # anything reaching this must be non-external. Soft-pass it.
        from systemu.runtime.shadow_runtime import CompletionOutcome, ObjectiveState
        return CompletionOutcome(credited=True, state=ObjectiveState())

    snap = _mk_snapshot(*_build_entities_objs(tmp_path, objectives)[1:],
                        exec_id="exec_s4r_confirmed", graph=graph, completed=[2],
                        external_evidence={"1": {"objective_id": 1, "confirmed": True}})
    captured = _run_resume_capture(
        tmp_path, monkeypatch, snap=snap, objectives=objectives,
        recredit_spy=_spy_recredit)

    assert 1 in captured.get("completed", set()), (
        "an external objective WITH a stored confirmed bit must be re-credited on "
        f"resume; completed={captured.get('completed')}")
    assert 1 not in calls["ids"], (
        "the local epoch-delta verifier must NOT be invoked for a stored-confirmed "
        f"external objective (no re-fetch); verifier saw ids {calls['ids']}")


def test_resume_external_readback_exception_credits_nothing(tmp_path, monkeypatch):
    """(3) an exception in the resume credit path for an external objective → not
    re-credited (fail-closed). We force _read_external_ok to raise; the outer
    try/except in the resume loop must degrade to 'not credited', never crediting."""
    import systemu.runtime.shadow_runtime as _sr
    from systemu.core.models import Objective

    graph = [
        _external_obj(id=1, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]
    objectives = [
        _external_obj(id=1, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]

    def _boom(context, objective_id):
        raise RuntimeError("readback exploded")

    monkeypatch.setattr(_sr, "_read_external_ok", _boom)

    snap = _mk_snapshot(*_build_entities_objs(tmp_path, objectives)[1:],
                        exec_id="exec_s4r_boom", graph=graph, completed=[2],
                        external_evidence={"1": {"objective_id": 1, "confirmed": True}})
    captured = _run_resume_capture(
        tmp_path, monkeypatch, snap=snap, objectives=objectives,
        recredit_spy=_softpass_recredit)
    assert 1 not in captured.get("completed", set()), (
        "an exception in the external resume-credit path must fail closed (not "
        f"credit); completed={captured.get('completed')}")


def test_resume_non_external_still_recredits(tmp_path, monkeypatch):
    """(4) a NON-external objective with durable evidence still re-credits on
    resume via the unchanged recredit_on_resume path (keeps the
    test_v0_9_1_verifiers recredit semantics green)."""
    from systemu.core.models import Objective
    graph = [
        Objective(id=1, goal="write the local file", success_criteria="file exists",
                  requires_external_verification=False, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]
    objectives = [
        Objective(id=1, goal="write the local file", success_criteria="file exists",
                  requires_external_verification=False, depends_on=[]),
        Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[]),
    ]
    snap = _mk_snapshot(*_build_entities_objs(tmp_path, objectives)[1:],
                        exec_id="exec_s4r_nonext", graph=graph, completed=[2],
                        external_evidence={})
    captured = _run_resume_capture(
        tmp_path, monkeypatch, snap=snap, objectives=objectives,
        recredit_spy=_softpass_recredit)
    assert 1 in captured.get("completed", set()), (
        "a non-external objective must still re-credit on resume via the unchanged "
        f"recredit path; completed={captured.get('completed')}")
