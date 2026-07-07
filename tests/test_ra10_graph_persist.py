"""R-A10 step B5 (RISK-2): the objective-graph persistence round-trip.

G1 persists ``ExecutionSnapshot.objective_graph`` but nothing WRITES
``context._objective_graph`` today, so a mutated graph (planner insert /
param-sub) would NOT survive the next snapshot. B5 closes that round-trip with a
CONDITIONAL write:

  * a run whose post-fold ``objectives`` DIVERGED from the static scroll tree
    (by identity) → capture persists the NON-empty graph, so the mutation
    survives the next snapshot;
  * a never-mutated run leaves ``context._objective_graph`` UNSET → capture
    persists ``[]`` → resume takes ``_resolve_objectives_for_run`` branch 3
    (identity) → AC6 byte-identical schedule is UNCHANGED vs G1.

Plus the resume re-seed: a resume from a snapshot with a NON-empty
``objective_graph`` re-seeds ``context._objective_graph`` so a SECOND capture
re-persists the graph (not ``[]``), and re-seeds ``context._requirement_report``
from ``snap.requirement_report`` (the B6 resume-restore, mirroring G1's peel).

Harness modelled on tests/test_g1_resume_graph.py.
"""
import pytest
from unittest.mock import patch


# ─────────────────────────────────────────────────────────────────────────────
# Shared entity builder (mirrors test_g1_resume_graph._build_resume_entities):
# a scroll whose STATIC tree has ONLY id=1, so a persisted graph carrying id=2
# proves the divergence path.
# ─────────────────────────────────────────────────────────────────────────────

def _build_resume_entities(tmp_path):
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

    shadow = Shadow(id="shadow_b5", name="B5 Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_b5", name="seed_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/seed_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_b5", name="B5 Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="root", success_criteria="Done")])
    vault.save_scroll(scroll)
    activity = Activity(id="act_b5", name="B5 Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_b5"], required_skill_ids=[],
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


# ─────────────────────────────────────────────────────────────────────────────
# 1. MUTATED persists — a run whose post-fold objectives diverged from the
#    scroll tree writes context._objective_graph, so capture_from_context carries
#    a NON-empty objective_graph.
#
#    We drive the REAL execute() through a resume that folds a persisted graph
#    (id=1 + inserted id=2). The fold returns objectives that are NOT
#    scroll.objectives by identity → the conditional write fires → we assert
#    context._objective_graph AND a snapshot captured from that context.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mutated_run_persists_nonempty_graph(tmp_path, monkeypatch):
    from sharing_on.config import Config
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import (
        ExecutionSnapshot, write_snapshot, capture_from_context,
    )
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_resume_entities(tmp_path)

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    exec_id = "exec_b5_mut"
    graph = [
        Objective(id=1, goal="root", success_criteria="Done"),
        Objective(id=2, goal="inserted", success_criteria="Done2",
                  depends_on=[1], origin="backchain"),
    ]
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b5",
        activity_id=activity.id, iteration=1, completed_objective_ids=[],
        objective_graph=graph, next_objective_id=3,
    )
    write_snapshot(snap, data_dir=data_dir)

    # Capture the runtime context after the fold has run (the conditional write
    # point) via the fold spy.
    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        return objs, sj

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    decisions = [{"action": "FAIL", "reason": "done — checking graph write"}]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(cfg, vault)
        await runtime.execute(shadow, activity, resume_from_execution_id=exec_id)

    ctx = captured.get("context")
    assert ctx is not None, "fold never reached — resume block did not run"
    # The conditional write fired because the post-fold objectives (from the
    # persisted graph) diverged from the static scroll tree by identity.
    graph_on_ctx = getattr(ctx, "_objective_graph", None)
    assert graph_on_ctx, "mutated run must WRITE a non-empty context._objective_graph"
    assert [o["id"] for o in graph_on_ctx] == [1, 2], graph_on_ctx

    # And a snapshot captured from that context carries the NON-empty graph, so
    # the mutation survives the NEXT snapshot (round-trip closed).
    out_snap = capture_from_context(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b5",
        iteration=2, current_action_block=1, completed_objectives=set(),
        context=ctx,
    )
    assert [o.id for o in out_snap.objective_graph] == [1, 2]


# ─────────────────────────────────────────────────────────────────────────────
# 2. NEVER-MUTATED persists [] (AC6) — a plain objectives run (no persisted
#    graph, no param-sub) must leave context._objective_graph UNSET, so a
#    snapshot captured from that context has objective_graph == [] (UNCHANGED
#    vs G1). This is the AC6-safety half of the conditional write.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_never_mutated_run_persists_empty_graph(tmp_path, monkeypatch):
    from sharing_on.config import Config
    from systemu.runtime.execution_snapshot import capture_from_context
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_resume_entities(tmp_path)

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    # NO resume, NO param-sub → the fold takes branch 3 (identity) → the write
    # must NOT fire. Spy the fold to grab the live context after the fold.
    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        # Sanity: this is the identity (no-mutation) branch.
        captured["identity"] = objs is kw.get("objectives")
        return objs, sj

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    decisions = [{"action": "FAIL", "reason": "done — checking no-write"}]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(cfg, vault)
        await runtime.execute(shadow, activity)

    ctx = captured.get("context")
    assert ctx is not None, "fold never reached"
    assert captured.get("identity") is True, "expected the identity (no-mutation) fold branch"
    # The conditional write did NOT fire → attribute unset (or falsy).
    assert not getattr(ctx, "_objective_graph", None), \
        "a never-mutated run must NOT write context._objective_graph"

    # A snapshot captured from that context persists an EMPTY graph — byte-for-byte
    # what G1 persisted before B5 (the AC6-preserving invariant).
    out_snap = capture_from_context(
        execution_id="exec_b5_plain", shadow_id=shadow.id, scroll_id="scroll_b5",
        iteration=1, current_action_block=1, completed_objectives=set(),
        context=ctx,
    )
    assert out_snap.objective_graph == [], out_snap.objective_graph


# ─────────────────────────────────────────────────────────────────────────────
# 3. AC6 snapshot BYTES unchanged by B5 — a never-mutated run's captured
#    snapshot dict must be byte-identical to one captured from a context that
#    has NEVER seen B5's write (i.e. objective_graph field == []). Guards against
#    B5 accidentally leaking a non-empty graph into the no-mutation snapshot.
# ─────────────────────────────────────────────────────────────────────────────

def test_ac6_snapshot_bytes_unchanged_for_no_mutation():
    import json
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import (
        capture_from_context, _to_dict,
    )
    from systemu.runtime.context_builder import ExecutionContext

    objs = [Objective(id=1, goal="g", success_criteria="s")]
    sj = [o.model_dump(mode="json") for o in objs]

    # A context that mirrors a never-mutated run: _objective_graph is UNSET.
    ctx = ExecutionContext(
        execution_id="exec_ac6", system_prompt="p", scroll_json=sj,
        tool_index=[], use_objectives=True,
    )
    assert not hasattr(ctx, "_objective_graph"), \
        "precondition: a fresh no-mutation context has no _objective_graph"

    snap = capture_from_context(
        execution_id="exec_ac6", shadow_id="sh", scroll_id="sc",
        iteration=1, current_action_block=1, completed_objectives=set(),
        context=ctx,
    )
    d = _to_dict(snap)
    # The AC6 invariant: the persisted graph is [] for a never-mutated run — the
    # exact bytes G1 wrote. B5 must not change this.
    assert d["objective_graph"] == []
    # And the whole dict round-trips through JSON cleanly (no B5-introduced junk).
    json.dumps(d, sort_keys=True)


# ─────────────────────────────────────────────────────────────────────────────
# 4. RESUME re-seed — a resume from a snapshot with a NON-empty objective_graph
#    must re-seed context._objective_graph so a SECOND capture re-persists the
#    graph (not []), and re-seed context._requirement_report from
#    snap.requirement_report (the B6 resume-restore).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_reseeds_graph_and_requirement_report(tmp_path, monkeypatch):
    from sharing_on.config import Config
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import (
        ExecutionSnapshot, write_snapshot, capture_from_context,
    )
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_resume_entities(tmp_path)

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    exec_id = "exec_b5_reseed"
    graph = [
        Objective(id=1, goal="root", success_criteria="Done"),
        Objective(id=2, goal="inserted", success_criteria="Done2",
                  depends_on=[1], origin="backchain"),
    ]
    req_report = {"decision": "PROCEED", "missing": [], "note": "b6 cache"}
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b5",
        activity_id=activity.id, iteration=1, completed_objective_ids=[],
        objective_graph=graph, next_objective_id=3,
        requirement_report=req_report,
    )
    write_snapshot(snap, data_dir=data_dir)

    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        return objs, sj

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    decisions = [{"action": "FAIL", "reason": "done — checking reseed"}]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(cfg, vault)
        await runtime.execute(shadow, activity, resume_from_execution_id=exec_id)

    ctx = captured.get("context")
    assert ctx is not None, "fold never reached — resume block did not run"

    # The requirement_report was re-seeded from the snapshot (B6 resume-restore).
    assert getattr(ctx, "_requirement_report", None) == req_report, \
        getattr(ctx, "_requirement_report", None)

    # A SECOND capture from the resumed context re-persists the graph, not [] —
    # so a snapshot-then-resume-then-snapshot chain keeps the graph durable.
    out_snap = capture_from_context(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b5",
        iteration=2, current_action_block=1, completed_objectives=set(),
        context=ctx,
    )
    assert [o.id for o in out_snap.objective_graph] == [1, 2], out_snap.objective_graph
    assert out_snap.requirement_report == req_report


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fix 1 (read-path poison guard) — a snapshot whose on-disk objective_graph
#    holds one VALID + one MALFORMED (field-missing) entry must NOT collapse the
#    whole read to None (the DEC-9 re-execute-effects hazard). Instead read
#    drops-with-warning (mirroring capture's _coerce_objectives) → read_snapshot
#    returns NON-None with the valid objective retained, the bad one dropped.
#
#    BEFORE the fix: the bare `[Objective(**o) for o in ...]` comprehension raises
#    on the malformed entry → caught by the broad except → read_snapshot returns
#    None → the resume caller starts fresh (re-executing effects). This test FAILS
#    (asserts None) before, passes after.
# ─────────────────────────────────────────────────────────────────────────────

def test_read_snapshot_drops_malformed_objective_keeps_valid(tmp_path):
    import json
    from systemu.runtime.execution_snapshot import (
        _snapshot_path, read_snapshot, CURRENT_SCHEMA_VERSION,
    )

    exec_id = "exec_b5_poison"
    target = _snapshot_path(tmp_path, exec_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    # One valid Objective + one malformed (missing the required `goal`/`success_criteria`).
    on_disk = {
        "execution_id": exec_id,
        "shadow_id": "sh",
        "scroll_id": "sc",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "next_objective_id": 3,
        "objective_graph": [
            {"id": 1, "goal": "root", "success_criteria": "Done"},
            {"id": 2},  # malformed: missing goal + success_criteria
        ],
    }
    target.write_text(json.dumps(on_disk), encoding="utf-8")

    snap = read_snapshot(exec_id, data_dir=tmp_path)
    # The poison entry must NOT collapse the read to None (the fresh-restart /
    # re-execute-effects hazard).
    assert snap is not None, (
        "a single malformed objective must NOT collapse the whole read to None "
        "(would silently start fresh + re-execute effects)"
    )
    # The valid objective is retained; the malformed one dropped-with-warning.
    assert [o.id for o in snap.objective_graph] == [1], snap.objective_graph
    assert snap.objective_graph[0].goal == "root"


def test_read_snapshot_non_list_objective_graph_degrades_to_empty(tmp_path):
    """The top-level list-type guard (mirrors situation_report/requirement_report):
    a non-list objective_graph on disk degrades to [] rather than crashing the read."""
    import json
    from systemu.runtime.execution_snapshot import (
        _snapshot_path, read_snapshot, CURRENT_SCHEMA_VERSION,
    )

    exec_id = "exec_b5_poison_nonlist"
    target = _snapshot_path(tmp_path, exec_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    on_disk = {
        "execution_id": exec_id,
        "shadow_id": "sh",
        "scroll_id": "sc",
        "schema_version": CURRENT_SCHEMA_VERSION,
        "objective_graph": {"not": "a list"},  # garbage type
    }
    target.write_text(json.dumps(on_disk), encoding="utf-8")

    snap = read_snapshot(exec_id, data_dir=tmp_path)
    assert snap is not None
    assert snap.objective_graph == []


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fix 3 (apply_to_context honors its "self-contained" docstring) — an
#    apply_to_context → capture_from_context cycle from a snapshot carrying
#    next_objective_id=N (N>1) must PRESERVE next_objective_id==N for a caller
#    relying SOLELY on the helper (it was collapsing to 1 because apply re-seeded
#    the graph but not context._next_objective_id).
#
#    BEFORE the fix: apply never sets context._next_objective_id → capture reads
#    the default 1 → this asserts ==N and FAILS. After: apply re-seeds it → passes.
#    (The live shadow_runtime path is unaffected — it recomputes
#    max(_resume_next_objective_id, _floor) AFTER apply, so AC6 bytes are unchanged.)
# ─────────────────────────────────────────────────────────────────────────────

def test_apply_then_capture_preserves_next_objective_id():
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import (
        ExecutionSnapshot, apply_to_context, capture_from_context,
    )
    from systemu.runtime.context_builder import ExecutionContext

    graph = [
        Objective(id=1, goal="root", success_criteria="Done"),
        Objective(id=2, goal="inserted", success_criteria="Done2",
                  depends_on=[1], origin="backchain"),
    ]
    snap = ExecutionSnapshot(
        execution_id="exec_nid", shadow_id="sh", scroll_id="sc",
        objective_graph=graph, next_objective_id=7,
    )

    ctx = ExecutionContext(
        execution_id="exec_nid", system_prompt="p",
        scroll_json=[o.model_dump(mode="json") for o in graph],
        tool_index=[], use_objectives=True,
    )
    apply_to_context(snap, context=ctx)

    # apply re-seeded the allocator floor (docstring: self-contained + idempotent).
    assert getattr(ctx, "_next_objective_id", None) == 7

    out_snap = capture_from_context(
        execution_id="exec_nid", shadow_id="sh", scroll_id="sc",
        iteration=1, current_action_block=1, completed_objectives=set(),
        context=ctx,
    )
    # A standalone apply→capture cycle no longer collapses N → 1.
    assert out_snap.next_objective_id == 7, out_snap.next_objective_id
    # And the graph round-trips too (unchanged by this fix).
    assert [o.id for o in out_snap.objective_graph] == [1, 2]


# ─────────────────────────────────────────────────────────────────────────────
# 7. Fix 2 (de-dup resume re-seed) — apply_to_context is now the single
#    authority for the graph + requirement_report re-seed on the resume path (the
#    redundant shadow_runtime block was deleted). This test confirms a resume from
#    a snapshot with requirement_report=None does NOT clobber a value apply would
#    otherwise set, AND that the surviving apply-authority re-seeds correctly.
#
#    Concretely: apply_to_context guards its requirement_report re-seed on
#    `is not None`, so a None snapshot leaves a pre-set context._requirement_report
#    UNTOUCHED — the divergence the deleted block would have introduced (an
#    UNCONDITIONAL None write clobbering a real report) is gone.
# ─────────────────────────────────────────────────────────────────────────────

def test_apply_none_requirement_report_does_not_clobber_preset():
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, apply_to_context
    from systemu.runtime.context_builder import ExecutionContext

    graph = [Objective(id=1, goal="root", success_criteria="Done")]
    # Snapshot carries NO requirement_report (None) — the B10-reachable divergence.
    snap = ExecutionSnapshot(
        execution_id="exec_noreq", shadow_id="sh", scroll_id="sc",
        objective_graph=graph, next_objective_id=2, requirement_report=None,
    )

    ctx = ExecutionContext(
        execution_id="exec_noreq", system_prompt="p",
        scroll_json=[o.model_dump(mode="json") for o in graph],
        tool_index=[], use_objectives=True,
    )
    # A real report is already on the context (as a B10 producer would leave it).
    preset = {"decision": "PROCEED", "note": "real report — must survive"}
    ctx._requirement_report = preset

    apply_to_context(snap, context=ctx)

    # apply's `is not None` guard means a None snapshot leaves the pre-set report intact.
    assert getattr(ctx, "_requirement_report", None) == preset, \
        getattr(ctx, "_requirement_report", None)


@pytest.mark.asyncio
async def test_resume_reseed_survives_single_authority(tmp_path, monkeypatch):
    """After de-duping the resume re-seed onto apply_to_context (single authority),
    a resume from a NON-empty-graph snapshot still re-seeds context._objective_graph
    and context._requirement_report — i.e. deleting the redundant shadow_runtime
    block did not regress the resume re-seed (test #4 covers the same, kept green)."""
    from sharing_on.config import Config
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import (
        ExecutionSnapshot, write_snapshot, capture_from_context,
    )
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_resume_entities(tmp_path)

    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    exec_id = "exec_b5_dedup"
    graph = [
        Objective(id=1, goal="root", success_criteria="Done"),
        Objective(id=2, goal="inserted", success_criteria="Done2",
                  depends_on=[1], origin="backchain"),
    ]
    req_report = {"decision": "PROCEED", "missing": [], "note": "b6 cache"}
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b5",
        activity_id=activity.id, iteration=1, completed_objective_ids=[],
        objective_graph=graph, next_objective_id=3,
        requirement_report=req_report,
    )
    write_snapshot(snap, data_dir=data_dir)

    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        return objs, sj

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    decisions = [{"action": "FAIL", "reason": "done — checking dedup reseed"}]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        runtime = ShadowRuntime(cfg, vault)
        await runtime.execute(shadow, activity, resume_from_execution_id=exec_id)

    ctx = captured.get("context")
    assert ctx is not None, "fold never reached — resume block did not run"
    # Single-authority (apply_to_context) still re-seeds both fields.
    assert getattr(ctx, "_requirement_report", None) == req_report
    graph_on_ctx = getattr(ctx, "_objective_graph", None)
    assert graph_on_ctx and [o["id"] for o in graph_on_ctx] == [1, 2], graph_on_ctx

    out_snap = capture_from_context(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b5",
        iteration=2, current_action_block=1, completed_objectives=set(),
        context=ctx,
    )
    assert [o.id for o in out_snap.objective_graph] == [1, 2]
    assert out_snap.requirement_report == req_report
