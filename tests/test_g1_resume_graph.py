"""G1 (R-A2): resume rehydrates the objective graph; a refused (newer-schema)
snapshot fails honestly instead of silently starting fresh (which could re-run
side effects). A later task appends the graph-branch + AC6 tests to this file.

This file covers the DEC-9 safety property across the THREE resume callers that
read a snapshot:

  #1 shadow_runtime.execute() resume block  — the ONLY genuine fresh-vs-resume
     chokepoint. SAFETY fix: a SnapshotRefused there returns a failure dict
     (never falls through to a silent fresh start).
  #2 resume_on_decision._dispatch_resume     — VISIBILITY fix: refusal logs
     loudly, snap=None (degrade path); the resume it would dispatch is refused
     at #1, so no fresh start happens.
  #3 supervisor.resume_after_grant           — VISIBILITY fix: refusal logs
     loudly, still re-submits a RESUME (never fresh); #1 refuses that resume.
"""
import json
import logging

import pytest
from unittest.mock import patch

from systemu.runtime.snapshot_migrations import SnapshotRefused


def _seed_newer_snapshot(data_dir, execution_id="newer"):
    # read_snapshot resolves the path via _snapshot_path, which prefixes the id
    # with 'exec_' — so a hand-written file for id 'newer' lives in 'exec_newer'.
    exec_dir = data_dir / "audit" / f"exec_{execution_id}"
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / "resume_snapshot.json").write_text(json.dumps({
        "schema_version": 999, "execution_id": execution_id,
        "shadow_id": "s", "scroll_id": "sc",
    }), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — contract test: the substrate the three fixes depend on
# ─────────────────────────────────────────────────────────────────────────────

def test_read_snapshot_refuses_newer(tmp_path):
    from systemu.runtime.execution_snapshot import read_snapshot
    dd = tmp_path / "data"
    _seed_newer_snapshot(dd, "newer")
    with pytest.raises(SnapshotRefused):
        read_snapshot("newer", data_dir=dd)


# ─────────────────────────────────────────────────────────────────────────────
# Caller #1 (SAFETY) — shadow_runtime.execute() resume block  [BEHAVIORAL]
#
# Drives the REAL execute() through the `if resume_from_execution_id:` branch
# (proven harness borrowed from tests/test_harness_grant_resume_apply.py). The
# branch's first act is `read_snapshot(resume_from_execution_id)`; we monkeypatch
# execution_snapshot.read_snapshot to raise SnapshotRefused. The added inner
# try/except must then return the failure dict — NOT fall through to a fresh
# start (which would re-execute effects). Because the read raises before the
# first decision, no network / LLM call is reached.
# ─────────────────────────────────────────────────────────────────────────────

def _build_runtime_entities(tmp_path):
    """Vault + Shadow + Scroll(objectives) + Activity + one deployed tool —
    everything execute() needs to REACH the resume block."""
    from systemu.vault.vault import Vault
    from systemu.core.models import (
        Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType,
        Scroll, Objective,
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

    shadow = Shadow(id="shadow_g1", name="Resume Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_g1", name="seed_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/seed_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_g1", name="Resume Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="Use capability",
                                          success_criteria="Done")])
    vault.save_scroll(scroll)
    activity = Activity(id="act_g1", name="Resume Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_g1"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


@pytest.mark.asyncio
async def test_caller1_execute_refuses_newer_snapshot(tmp_path, monkeypatch):
    """SAFETY: execute() with resume_from_execution_id whose snapshot is refused
    returns status=failure ('resume refused'), NOT a silent fresh start."""
    from sharing_on.config import Config

    vault, shadow, activity = _build_runtime_entities(tmp_path)

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    def _raise_refused(execution_id, *a, **kw):
        raise SnapshotRefused(999, 2)

    # The resume block imports read_snapshot from execution_snapshot INSIDE the
    # branch; patch it at the source module so the bound name raises.
    import systemu.runtime.execution_snapshot as _es
    monkeypatch.setattr(_es, "read_snapshot", _raise_refused)

    with patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(cfg, vault)
        result = await runtime.execute(
            shadow, activity, resume_from_execution_id="refused-exec")

    assert result["status"] == "failure", result
    assert "resume refused" in result["error"], result
    assert "execution_id" in result
    # Structural: this build can't read a newer/garbage snapshot; re-running won't
    # fix it and retrying FRESH (supervisor drops resume_from_execution_id) would
    # re-execute the parked run's effects. The flag routes _should_retry → terminal.
    assert result.get("structural_failure") is True   # terminal — never retried fresh


# ─────────────────────────────────────────────────────────────────────────────
# Caller #1 downstream — the supervisor must NOT retry the refusal FRESH
#
# The refusal dict from execute() flows into Supervisor._handle_result. Without a
# structural_failure flag the generic failure→retry path fires a threading.Timer
# → submit() with NO resume_from_execution_id → the resume block is skipped → the
# parked run's objectives run from scratch (the DEC-9 hazard, relocated). Marking
# the refusal structural must route _should_retry → terminal (dead-letter), NEVER
# a fresh retry.
# ─────────────────────────────────────────────────────────────────────────────

def _make_handle_result_supervisor():
    """A Supervisor wired just enough to drive _handle_result's retry-vs-terminal
    decision (mirrors the stub in tests/test_v0_9_7_resume_after_grant.py)."""
    import queue as _queue
    import threading as _threading
    from types import SimpleNamespace
    from systemu.runtime.supervisor import Supervisor
    s = Supervisor.__new__(Supervisor)
    s.vault = None                       # _aname + mark_activity_failed tolerate None
    s._task_queue = None
    s._queue = _queue.PriorityQueue()
    s._dl_lock = _threading.Lock()
    s._dead_letters = []
    s._publish = lambda *a, **kw: None
    return s


def test_resume_refused_failure_is_terminal_not_retried_fresh(monkeypatch, tmp_path):
    """A 'resume refused' failure must be structural → _handle_result must NOT
    schedule a fresh retry submit (which would drop resume_from_execution_id and
    re-run objectives from scratch)."""
    from systemu.runtime.supervisor import Supervisor
    import systemu.runtime.supervisor as _sup

    # Direct: _should_retry is a staticmethod — structural is terminal.
    assert Supervisor._should_retry("failure", 0, structural=True) is False
    # And a NON-structural failure with retries left WOULD retry — this is exactly
    # the path the flag must keep our refusal off of.
    assert Supervisor._should_retry("failure", 0, structural=False) is True

    # Behavioral: drive _handle_result with the real refusal dict and assert the
    # retry Timer is NEVER constructed (the dead-letter path uses a Thread, not a
    # Timer, so patching Timer cleanly isolates the fresh-retry branch).
    timer_calls = []

    class _RecordingTimer:
        def __init__(self, *a, **kw):
            timer_calls.append((a, kw))

        def start(self):
            pass

    monkeypatch.setattr(_sup.threading, "Timer", _RecordingTimer)
    # Neutralize the background diagnosis thread the dead-letter branch spawns.
    monkeypatch.setattr(Supervisor, "_analyze_failure", lambda self, p, r: None)

    sup = _make_handle_result_supervisor()
    payload = {"activity_id": "act_x", "shadow_id": "sh_x",
               "retry_count": 0, "origin": "chat"}
    refusal = {
        "status": "failure",
        "structural_failure": True,
        "error": "resume refused: snapshot schema 999 is unsupported",
        "execution_id": "exec_x",
    }

    sup._handle_result(payload, refusal)

    # No fresh retry was scheduled — the refusal went straight to terminal.
    assert timer_calls == [], timer_calls
    # It was dead-lettered as a structural blocker.
    assert len(sup._dead_letters) == 1, sup._dead_letters
    assert sup._dead_letters[0]["structural"] is True

    # Counter-proof of WHY the flag matters: the identical failure dict WITHOUT
    # the structural flag (what caller #1 returned before this fix) DOES retry.
    # R-A12a (timer→durable-wait migration): the retry is now armed as a DURABLE
    # ``pending_wait`` on the run's ExecutionSnapshot (which survives a restart),
    # not an in-process ``threading.Timer``. It is still a FRESH run — the record
    # carries NO resume_from_execution_id, so a reconciler resubmit re-runs
    # objectives from scratch. This is the exact hazard the structural flag closes.
    from systemu.runtime.execution_snapshot import read_snapshot
    timer_calls.clear()
    sup2 = _make_handle_result_supervisor()
    sup2._snapshot_data_dir = tmp_path   # R-A12a: isolate the durable wait at tmp
    unflagged = dict(refusal)
    unflagged.pop("structural_failure")
    sup2._handle_result(payload, unflagged)
    # No fresh-retry Timer — the retry is durable, deferred to the reconciler.
    assert timer_calls == []
    snap = read_snapshot("exec_x", data_dir=tmp_path)
    assert snap is not None and len(snap.pending_waits) == 1, \
        "unflagged failure must arm a durable retry (proves the flag is load-bearing)"
    # The durable record carries NO resume hint → the reconciler resubmits FRESH.
    assert "resume_from_execution_id" not in snap.pending_waits[0]


# ─────────────────────────────────────────────────────────────────────────────
# Caller #2 (VISIBILITY) — resume_on_decision._dispatch_resume
#
# Behavioral test through the real handler. A gate decision carries execution_id
# but NOT activity_id/shadow_id, so the handler must derive them from the parked
# run's snapshot. With a REFUSED (v999) snapshot on disk, the handler must (a)
# log the refusal loudly and (b) NOT dispatch a fresh run (it returns False
# because the resume coords can't be resolved). No FakeSupervisor.submit fires.
# ─────────────────────────────────────────────────────────────────────────────

def _make_vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in [
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications",
        "executions", "decisions",
    ]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in [
        "scrolls", "activities", "shadow_army", "skills", "tools",
        "evolutions", "decisions",
    ]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(tmp_path))


class _FakeSupervisor:
    """Records submit() calls so we can assert exact dispatch counts."""
    def __init__(self):
        self.calls = []

    def submit(self, activity_id, shadow_id, **kw):
        self.calls.append((activity_id, shadow_id, kw))
        return f"sub_{len(self.calls)}"


def test_caller2_dispatch_resume_refuses_newer_snapshot(tmp_path, caplog):
    """VISIBILITY: a refused snapshot logs loudly and yields ZERO fresh
    dispatches (the coords can't be derived → the handler declines)."""
    from systemu.runtime import resume_on_decision as rod
    from systemu.approval.decision_queue import OperatorDecisionQueue

    rod._handled.clear()

    vlt = _make_vault(tmp_path)
    data_dir = tmp_path / "data"
    _seed_newer_snapshot(data_dir, "exec_R2")

    queue = OperatorDecisionQueue(vlt)
    # A tool gate: carries execution_id but NOT activity_id/shadow_id, so the
    # handler MUST fall back to the (now refused) snapshot to derive them.
    did = queue.post(
        title="Tool gate", body="approve tool?",
        options=["Approve once", "Always allow", "Deny"],
        context={
            "kind": "gate",
            "gate_type": "tool",
            "chat_submission_id": "ts-R2",
            "execution_id": "exec_R2",
            "tool_signature": "sig-abc",
            # activity_id / shadow_id intentionally absent
        },
        dedup_key="gate:tool:R2",
    )
    queue.resolve(did, choice="Approve once")

    sup = _FakeSupervisor()
    with caplog.at_level(logging.ERROR):
        dispatched = rod._dispatch_resume(
            vlt.get_decision(did), vault=vlt, supervisor=sup, data_dir=data_dir,
        )

    # No fresh dispatch — the handler declined because coords were unresolvable.
    assert dispatched is False
    assert sup.calls == []
    # And it logged the refusal loudly (not a silent degrade-to-None).
    assert any(
        "snapshot refused" in r.getMessage().lower()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ─────────────────────────────────────────────────────────────────────────────
# Caller #3 (VISIBILITY) — supervisor.resume_after_grant
#
# Behavioral test through the real method. read_snapshot (imported inside the
# method from execution_snapshot) is patched to raise SnapshotRefused. The
# method must (a) log the refusal loudly and (b) STILL re-submit — as a RESUME
# (resume_from_execution_id set), NOT a fresh run. #1 refuses that resume.
# ─────────────────────────────────────────────────────────────────────────────

def _supervisor_stub(vault=None):
    import queue as _queue
    import threading
    from types import SimpleNamespace
    from systemu.runtime.supervisor import Supervisor
    s = Supervisor.__new__(Supervisor)
    s.vault = vault or SimpleNamespace()
    s._pending_lock = threading.Lock()
    s._pending_activity_ids = set()
    s._running_lock = threading.Lock()
    s._running = {}
    s._task_queue = None
    s._queue = _queue.PriorityQueue()
    s._publish = lambda *a, **kw: None
    return s


def test_caller3_resume_after_grant_refuses_newer_snapshot(tmp_path, monkeypatch, caplog):
    """VISIBILITY: a refused snapshot logs loudly, and resume_after_grant still
    re-submits a RESUME (resume_from_execution_id set) — never a fresh start.
    The dispatched resume is refused at the #1 chokepoint (execute)."""
    import systemu.runtime.execution_snapshot as _es

    def _raise_refused(execution_id, *a, **kw):
        raise SnapshotRefused(999, 2)

    monkeypatch.setattr(_es, "read_snapshot", _raise_refused)

    submit_calls = []
    sup = _supervisor_stub()
    monkeypatch.setattr(
        sup, "submit",
        lambda activity_id, shadow_id, **kw: submit_calls.append(
            (activity_id, shadow_id, kw)) or "sub_c3",
    )

    with caplog.at_level(logging.ERROR):
        sub_id = sup.resume_after_grant(
            execution_id="exec_C3",
            activity_id="act_C3",
            shadow_id="sh_C3",
            grant_payload={"granted_tool": "geocode"},
        )

    # It still re-submitted — and as a RESUME, not a fresh start.
    assert len(submit_calls) == 1, submit_calls
    aid, sid, kw = submit_calls[0]
    assert aid == "act_C3"
    assert sid == "sh_C3"
    assert kw["resume_from_execution_id"] == "exec_C3"
    assert sub_id == "sub_c3"
    # And it logged the refusal loudly (not the vague "best-effort" line).
    assert any(
        "refused" in r.getMessage().lower()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


# ── Task 5: _resolve_objectives_for_run + the AC6 byte-identical floor ──────────

def test_ac6_no_graph_no_paramsub_preserves_objectives_by_identity():
    """AC6 (SPEC §5.2): a never-mutated run must produce a byte-identical schedule.
    pending_objs is a pure function of `objectives`, so the invariant reduces to:
    with no persisted graph and no param-substitution, _resolve_objectives_for_run
    returns the SAME objects it was given (by identity — no rebuild, no perturbation)."""
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    class _Ctx:
        scroll_json = None    # no param-substitution grant

    objs = [Objective(id=1, goal="g", success_criteria="s")]
    sj = [o.model_dump(mode="json") for o in objs]
    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=None,
    )
    assert out_objs is objs           # SAME list object — no rebuild
    assert out_sj is sj

    # An EMPTY persisted graph is also a strict no-op (falsy → fallback).
    out_objs2, out_sj2 = _resolve_objectives_for_run(
        use_objectives=True, objectives=objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=[],
    )
    assert out_objs2 is objs
    assert out_sj2 is sj


def test_persisted_graph_wins_and_rebuilds():
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    class _Ctx:
        scroll_json = None

    scroll_objs = [Objective(id=1, goal="g", success_criteria="s")]
    sj = [o.model_dump(mode="json") for o in scroll_objs]
    graph = [
        Objective(id=1, goal="g", success_criteria="s"),
        Objective(id=2, goal="inserted", success_criteria="s2",
                  depends_on=[1], origin="backchain"),
    ]
    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=graph,
    )
    assert [o.id for o in out_objs] == [1, 2]        # persisted graph, not scroll tree
    assert out_objs[1].origin == "backchain"
    assert [o["id"] for o in out_sj] == [1, 2]


def test_paramsub_still_works_when_no_graph():
    """The v0.9.35 param-substitution seam-fix must survive the fold."""
    from systemu.runtime.shadow_runtime import _resolve_objectives_for_run
    from systemu.core.models import Objective

    scroll_objs = [Objective(id=1, goal="g", success_criteria="s")]
    sj = [o.model_dump(mode="json") for o in scroll_objs]
    substituted = [Objective(id=1, goal="g", success_criteria="s", hints={"x": "y"})]
    sub_sj = [o.model_dump(mode="json") for o in substituted]

    class _Ctx:
        scroll_json = sub_sj    # operator substituted values → a DIFFERENT object

    out_objs, out_sj = _resolve_objectives_for_run(
        use_objectives=True, objectives=scroll_objs, scroll_json=sj,
        context=_Ctx(), resume_objective_graph=None,
    )
    assert out_objs[0].hints == {"x": "y"}    # rebuilt from context.scroll_json
    assert out_sj is sub_sj


# ── Task 5: end-to-end — a real resume drives the run from the persisted graph ──

def _build_resume_entities(tmp_path):
    """Vault + Shadow + Scroll(only id=1) + Activity + one deployed tool —
    everything execute() needs to REACH the resume block and the objectives fold.
    Mirrors tests/test_v0937_resume_reconciliation.py's runtime_setup."""
    from systemu.vault.vault import Vault
    from systemu.core.models import (
        Activity, Shadow, ShadowStatus, Tool, ToolStatus, ToolType,
        Scroll, Objective,
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

    shadow = Shadow(id="shadow_g1e", name="Resume Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_g1e", name="seed_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/seed_tool.py")
    vault.save_tool(tool)
    # Scroll's STATIC tree has ONLY id=1 — proving the resume drove from the
    # GRAPH (which carries id=2), not the scroll, requires id=2 to appear.
    scroll = Scroll(id="scroll_g1e", name="Resume Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="root", success_criteria="Done")])
    vault.save_scroll(scroll)
    activity = Activity(id="act_g1e", name="Resume Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_g1e"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


@pytest.mark.asyncio
async def test_resume_rehydrates_inserted_objective(tmp_path, monkeypatch):
    """FULL DRIVE: a snapshot whose objective_graph carries an inserted id=2 (the
    scroll has only id=1) must, on resume through the REAL execute(), drive the run
    from the persisted GRAPH — id=2 present in the loop's `objectives`, and withheld
    from the schedule (pending_objs) until id=1 completes. Proves the graph, not the
    static scroll tree, wins, and that depends_on edges survive restart."""
    from sharing_on.config import Config
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_resume_entities(tmp_path)

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    # Redirect snapshot I/O to a test data_dir (same technique as the
    # reconciliation harness) so the real resume block reads OUR seeded snapshot.
    data_dir = tmp_path / "snap_data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot", lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot", lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot", lambda eid, **kw: rd(eid, data_dir=data_dir))

    exec_id = "exec_g1e"
    graph = [
        Objective(id=1, goal="root", success_criteria="Done"),
        Objective(id=2, goal="inserted", success_criteria="Done2",
                  depends_on=[1], origin="backchain"),
    ]
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_g1e",
        activity_id=activity.id, iteration=1, completed_objective_ids=[],
        objective_graph=graph, next_objective_id=3,
    )
    write_snapshot(snap, data_dir=data_dir)

    # Spy on the module-level fold to capture the post-fold objectives the REAL
    # loop uses (this is the exact list pending_objs iterates over, top of loop).
    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["objectives"] = objs
        return objs, sj

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    # FAIL immediately at iteration 1 (id=1 NOT yet completed) — lightweight drive.
    decisions = [{"action": "FAIL", "reason": "done — checking graph rehydrate"}]

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(cfg, vault)
        await runtime.execute(shadow, activity, resume_from_execution_id=exec_id)

    # The real loop's objectives came from the persisted GRAPH (id=2 present),
    # NOT the static scroll tree (which has only id=1).
    objs = captured.get("objectives")
    assert objs is not None, "the fold was never reached — resume block did not run"
    assert [o.id for o in objs] == [1, 2], [o.id for o in objs]
    assert objs[1].origin == "backchain"
    assert objs[1].depends_on == [1]

    # depends_on honored: with id=1 not yet completed, the first-iteration schedule
    # (pending_objs is a pure function of objectives + completed_objectives) withholds
    # id=2 and offers only id=1.
    completed_first_iter = set()
    pending = [
        o.id for o in objs
        if o.id not in completed_first_iter
        and all(dep in completed_first_iter for dep in o.depends_on)
    ]
    assert pending == [1], pending      # id=2 withheld until id=1 completes
    # And once id=1 lands, id=2 becomes schedulable.
    pending_after = [
        o.id for o in objs
        if o.id not in {1}
        and all(dep in {1} for dep in o.depends_on)
    ]
    assert pending_after == [2], pending_after


def _redirect_snapshot_io(monkeypatch, data_dir):
    """Redirect execution_snapshot read/write/delete to a test data_dir so the
    REAL resume block in execute() reads our seeded snapshot. Mirrors the
    reconciliation harness's _redirect_snapshot."""
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot", lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot", lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot", lambda eid, **kw: rd(eid, data_dir=data_dir))


@pytest.mark.asyncio
async def test_resume_total_objectives_reflects_folded_graph(tmp_path, monkeypatch):
    """FULL DRIVE — Fix 1: the completion denominator must count the FOLDED
    objectives, not the stale scroll length. Scroll has 1 objective; the persisted
    graph has 2. The loop re-derives `total_objectives` after the fold, so the
    per-iteration EventBus event (a real downstream consumer that reads the
    loop-local `total_objectives`) must report objectives_total == 2, not 1.

    SEAM: EventBus 'shadow' iteration events (published every iteration from the
    loop, field 'objectives_total' = total_objectives). This FAILS if the
    re-derivation after the fold is removed (it would report the scroll's 1)."""
    from sharing_on.config import Config
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    from systemu.interface.event_bus import EventBus

    vault, shadow, activity = _build_resume_entities(tmp_path)

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    exec_id = "exec_g1e_total"
    graph = [
        Objective(id=1, goal="root", success_criteria="Done"),
        Objective(id=2, goal="inserted", success_criteria="Done2",
                  depends_on=[1], origin="backchain"),
    ]
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_g1e",
        activity_id=activity.id, iteration=1, completed_objective_ids=[],
        objective_graph=graph, next_objective_id=3,
    )
    write_snapshot(snap, data_dir=data_dir)

    # Capture objectives_total from the loop's per-iteration 'shadow' events.
    totals = []

    def _cap(ev):
        ctx = ev.get("context") or {}
        if ev.get("category") == "shadow" and "objectives_total" in ctx:
            totals.append(ctx["objectives_total"])

    unsub = EventBus.get().subscribe(_cap, replay=False)
    try:
        decisions = [{"action": "FAIL", "reason": "done — checking total_objectives"}]
        with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
             patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
            from systemu.runtime.shadow_runtime import ShadowRuntime
            runtime = ShadowRuntime(cfg, vault)
            await runtime.execute(shadow, activity, resume_from_execution_id=exec_id)
    finally:
        unsub()

    assert totals, "no per-iteration 'shadow' event with objectives_total was published"
    # The denominator reflects the FOLDED graph (2), not the stale scroll tree (1).
    assert all(t == 2 for t in totals), totals


@pytest.mark.asyncio
async def test_resume_next_objective_id_floor_hardens_corrupt_snapshot(tmp_path, monkeypatch):
    """FULL DRIVE — Fix 2: a corrupt/hand-edited snapshot whose next_objective_id
    (1) sits BELOW the graph's max id+1 must be floored UP to max(id)+1 so a future
    insert can't collide. Graph max id == 2 → floor == 3; the restored 1 loses.

    SEAM: context._next_objective_id, stashed by the fold block for the next
    snapshot's capture. Read it off the runtime context after a real resume drive."""
    from sharing_on.config import Config
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_resume_entities(tmp_path)

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    exec_id = "exec_g1e_floor"
    graph = [
        Objective(id=1, goal="root", success_criteria="Done"),
        Objective(id=2, goal="inserted", success_criteria="Done2",
                  depends_on=[1], origin="backchain"),
    ]
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_g1e",
        activity_id=activity.id, iteration=1, completed_objective_ids=[],
        objective_graph=graph,
        next_objective_id=1,       # CORRUPT: below max(id)+1 == 3
    )
    write_snapshot(snap, data_dir=data_dir)

    # Capture the runtime context the fold stashes _next_objective_id onto.
    captured_ctx = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        captured_ctx["context"] = kw.get("context")
        return orig_resolve(**kw)

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    decisions = [{"action": "FAIL", "reason": "done — checking id floor"}]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(cfg, vault)
        await runtime.execute(shadow, activity, resume_from_execution_id=exec_id)

    ctx = captured_ctx.get("context")
    assert ctx is not None, "the fold was never reached — resume block did not run"
    # The corrupt restored value (1) was floored UP to max(id)+1 == 3.
    assert ctx._next_objective_id == 3, ctx._next_objective_id


def test_next_objective_id_floor_unit():
    """Fix 2 (focused): the floor logic — restored value floored up to max(id)+1
    when corrupt; a legitimately-advanced restored value (>= floor) wins unchanged;
    None falls back to max(id)+1."""
    from systemu.core.models import Objective

    def _floor_of(objectives, restored):
        # Mirrors the fold block's hardened floor (shadow_runtime.py).
        floor = max((o.id for o in objectives), default=0) + 1
        return (max(restored, floor) if restored is not None else floor)

    objs = [Objective(id=1, goal="g", success_criteria="s"),
            Objective(id=2, goal="g2", success_criteria="s2")]
    assert _floor_of(objs, 1) == 3        # corrupt/below → floored up
    assert _floor_of(objs, 3) == 3        # exactly floor → unchanged
    assert _floor_of(objs, 9) == 9        # legitimately advanced → wins
    assert _floor_of(objs, None) == 3     # absent → max(id)+1
    assert _floor_of([], None) == 1       # empty → 1 (matches capture's 1-based floor)
