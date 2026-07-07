"""R-A9 Task 9 — the defensive, fail-safe pre-plan ``survey_situation`` stage
wired into the LIVE run path (``shadow_runtime.execute``).

Contract (this task adds NO consumer — the planner reads the report at R-A10):

  * The survey runs ONCE per run, AFTER the resume block + objectives fold and
    BEFORE the main loop. It stashes ``context._situation_report`` (a
    ``SituationReport.model_dump()``) + ``context._situation_stamps`` so
    ``capture_from_context`` persists them (Task 8 wiring).
  * FAIL-SAFE (AC7): ANY survey failure / timeout / exception leaves the run
    EXACTLY as it is today — no report, no crash, no hang, no NEW operator card.
  * NON-PERTURBING: the survey only touches ``context`` attrs; it never mutates
    ``objectives`` / the schedule, so the run outcome is unaffected.
  * RESUME cache (AC3 across resume): on a resume the cached ``(report, stamps)``
    from the snapshot feeds the survey's ``cache`` so unchanged slices are reused.

Harness mirrors tests/test_g1_resume_graph.py's e2e drive: a real ``execute()``
run driven to an immediate FAIL, with ``_resolve_objectives_for_run`` spied to
capture the runtime ``context`` (the survey stashes onto the SAME context right
after the fold).
"""
from __future__ import annotations

import time

import pytest
from unittest.mock import patch


# --------------------------------------------------------------------------- #
# Shared harness — a real vault/shadow/scroll/activity + snapshot-IO redirect.
# --------------------------------------------------------------------------- #
def _build_entities(tmp_path):
    """Vault + Shadow + Scroll(one objective) + Activity + one deployed tool —
    everything execute() needs to REACH the resume block, the objectives fold,
    and the pre-plan survey stage. Mirrors test_g1_resume_graph._build_resume_entities."""
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

    shadow = Shadow(id="shadow_ra9", name="Survey Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_ra9", name="seed_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/seed_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_ra9", name="Survey Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[Objective(id=1, goal="root", success_criteria="Done")])
    vault.save_scroll(scroll)
    activity = Activity(id="act_ra9", name="Survey Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_ra9"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


def _make_config(tmp_path):
    from sharing_on.config import Config
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    return cfg


def _grant_root_with_pdf(vault, tmp_path):
    """Grant a root under the vault base_dir and drop a salient PDF in it."""
    from systemu.runtime.granted_roots import GrantedRootsStore
    root = tmp_path / "Docs"
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.pdf").write_text("%PDF-1.4 ...")
    store = GrantedRootsStore(base_dir=vault.root)
    store.grant(str(root))
    return store, root


def _drive_capturing_context(cfg, vault, shadow, activity, monkeypatch,
                             *, resume_from=None):
    """Drive a real execute() to an immediate FAIL, spying the objectives fold to
    capture the runtime `context` the survey stashes onto. Returns (result, ctx)."""
    import systemu.runtime.shadow_runtime as _sr

    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        captured["context"] = kw.get("context")
        return orig_resolve(**kw)

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    import asyncio
    decisions = [{"action": "FAIL", "reason": "done — RA9 wiring probe"}]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        runtime = _sr.ShadowRuntime(cfg, vault)
        result = asyncio.run(
            runtime.execute(shadow, activity, resume_from_execution_id=resume_from))
    return result, captured.get("context")


# --------------------------------------------------------------------------- #
# AC1 (light e2e) — after the pre-plan stage, context carries a non-None
# situation_report with BOTH the connected service AND the granted root.
# --------------------------------------------------------------------------- #
def test_ac1_survey_stashes_report_on_context(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    vault, shadow, activity = _build_entities(tmp_path)
    url = "https://mcp.example.com/a"
    connections.add_server(vault, url)
    _store, root = _grant_root_with_pdf(vault, tmp_path)

    cfg = _make_config(tmp_path)
    _result, ctx = _drive_capturing_context(cfg, vault, shadow, activity, monkeypatch)

    assert ctx is not None, "the objectives fold was never reached"
    report = getattr(ctx, "_situation_report", None)
    assert report is not None, "the pre-plan survey did not stash a report on context"
    assert isinstance(report, dict), "report must be a model_dump() dict (store-agnostic)"

    # the connected service is present
    names = {s.get("name") for s in report.get("services", [])}
    assert url in names, report.get("services")
    # the granted root is present
    import os
    root_paths = {os.path.normcase(r.get("path", "")) for r in report.get("roots", [])}
    assert os.path.normcase(str(root)) in root_paths, report.get("roots")

    # stamps stashed too (feeds the resume cache).
    stamps = getattr(ctx, "_situation_stamps", None)
    assert isinstance(stamps, dict) and "roots" in stamps, stamps


# --------------------------------------------------------------------------- #
# AC1 persistence — the stash rides capture_from_context into the snapshot.
# --------------------------------------------------------------------------- #
def test_survey_report_persists_via_capture_from_context(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    from systemu.runtime.execution_snapshot import capture_from_context
    vault, shadow, activity = _build_entities(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    cfg = _make_config(tmp_path)
    _result, ctx = _drive_capturing_context(cfg, vault, shadow, activity, monkeypatch)
    assert ctx is not None

    snap = capture_from_context(
        execution_id="e", shadow_id="s", scroll_id="sc",
        iteration=1, current_action_block=1, completed_objectives=set(),
        context=ctx, activity_id="a",
    )
    assert snap.situation_report is not None
    assert "https://mcp.example.com/a" in {
        s.get("name") for s in snap.situation_report.get("services", [])}
    assert isinstance(snap.situation_stamps, dict) and snap.situation_stamps


# --------------------------------------------------------------------------- #
# AC7 (silent pass) — the survey posts NO new operator decision/card.
# --------------------------------------------------------------------------- #
def test_ac7_survey_posts_no_operator_decision(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    vault, shadow, activity = _build_entities(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    cfg = _make_config(tmp_path)
    _result, ctx = _drive_capturing_context(cfg, vault, shadow, activity, monkeypatch)
    assert ctx is not None
    assert getattr(ctx, "_situation_report", None) is not None  # survey DID run

    # No operator decision was created by the run (the survey is silent — AC7).
    # OperatorDecisions persist as files under the vault's decisions/ dir.
    import os
    dec_dir = vault.root / "decisions"
    decision_files = [
        f for f in os.listdir(dec_dir)
        if f.endswith(".json") and f != "index.json"
    ] if dec_dir.exists() else []
    assert decision_files == [], (
        f"the survey must post NO operator card; got {decision_files}")


# --------------------------------------------------------------------------- #
# FAIL-SAFE (raise) — a survey that RAISES leaves the run unchanged: no report,
# no crash, the run still FAILs cleanly.
# --------------------------------------------------------------------------- #
def test_failsafe_raising_survey_is_a_noop(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    import systemu.runtime.situational_inventory as _si
    vault, shadow, activity = _build_entities(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    async def _boom(*a, **k):
        raise RuntimeError("survey blew up")

    monkeypatch.setattr(_si, "survey_situation", _boom)

    cfg = _make_config(tmp_path)
    result, ctx = _drive_capturing_context(cfg, vault, shadow, activity, monkeypatch)

    # The run completed (did not crash) and reached the fold (ctx captured).
    assert ctx is not None
    assert result.get("status") == "failure"  # the FAIL decision path, unchanged
    # No report was stashed — the raise was a strict no-op.
    assert getattr(ctx, "_situation_report", None) is None


# --------------------------------------------------------------------------- #
# FAIL-SAFE (hang) — a survey that HANGS past the overall timeout wrapper must
# not hang the run. We shrink the wrapper via a fast-timeout monkeypatch of
# asyncio.wait_for so the test is bounded, and hang the survey past it.
# --------------------------------------------------------------------------- #
def test_failsafe_hanging_survey_times_out_and_is_a_noop(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    import systemu.runtime.situational_inventory as _si
    vault, shadow, activity = _build_entities(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    import asyncio as _asyncio

    async def _hang(*a, **k):
        await _asyncio.sleep(30)  # far past the (shrunk) wrapper timeout
        return None

    monkeypatch.setattr(_si, "survey_situation", _hang)

    # Shrink the overall timeout wrapper: the wiring calls asyncio.wait_for on the
    # survey coroutine; force any survey wait_for to a tiny bound so the hang is
    # cancelled fast. (Other wait_for calls in the run don't wrap _hang, so this
    # only fast-fails the survey.)
    _orig_wait_for = _asyncio.wait_for

    async def _fast_wait_for(aw, timeout=None):
        # The survey coroutine sleeps 30s; cap ALL wait_for at 0.3s here. The run's
        # other awaited work (a single FAIL decision) completes well under 0.3s.
        return await _orig_wait_for(aw, timeout=0.3)

    monkeypatch.setattr(_asyncio, "wait_for", _fast_wait_for)

    cfg = _make_config(tmp_path)
    start = time.monotonic()
    result, ctx = _drive_capturing_context(cfg, vault, shadow, activity, monkeypatch)
    elapsed = time.monotonic() - start

    # Bounded: the run did NOT wait on the 30s hang.
    assert elapsed < 10, f"the survey hang stalled the run ({elapsed:.1f}s)"
    assert ctx is not None
    assert result.get("status") == "failure"  # run finished normally
    # The timed-out survey stashed no report — strict no-op.
    assert getattr(ctx, "_situation_report", None) is None


# --------------------------------------------------------------------------- #
# NO-PERTURBATION — the survey stash does NOT change the objective schedule.
# The captured objectives (post-fold) are the SAME static scroll tree they would
# be without the survey; the run outcome is identical.
# --------------------------------------------------------------------------- #
def test_survey_does_not_perturb_objective_schedule(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    import systemu.runtime.shadow_runtime as _sr
    vault, shadow, activity = _build_entities(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["objectives"] = objs
        captured["context"] = kw.get("context")
        return objs, sj

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    import asyncio
    decisions = [{"action": "FAIL", "reason": "done — schedule probe"}]
    cfg = _make_config(tmp_path)
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        runtime = _sr.ShadowRuntime(cfg, vault)
        result = asyncio.run(runtime.execute(shadow, activity))

    objs = captured.get("objectives")
    assert objs is not None
    # The schedule is EXACTLY the static scroll tree (id=1 only) — the survey
    # added nothing to `objectives`.
    assert [o.id for o in objs] == [1], [o.id for o in objs]
    assert result.get("status") == "failure"
    # And the survey still stashed its report on the SAME context (additive).
    assert getattr(captured.get("context"), "_situation_report", None) is not None


# --------------------------------------------------------------------------- #
# RESUME cache — on a resume, the cached (report, stamps) from the snapshot feeds
# the survey's cache, so a slice with an unchanged stamp is REUSED (not rebuilt).
# We seed a snapshot carrying situation_report/situation_stamps whose stamps match
# the live sources, then assert the reused-slice builders are NOT re-invoked.
# --------------------------------------------------------------------------- #
def test_resume_cache_feeds_survey_and_reuses_unchanged_slices(tmp_path, monkeypatch):
    from systemu.runtime.mcp import connections
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    import systemu.runtime.situational_inventory as _si
    import asyncio

    vault, shadow, activity = _build_entities(tmp_path)
    connections.add_server(vault, "https://mcp.example.com/a")
    _grant_root_with_pdf(vault, tmp_path)

    # Redirect snapshot IO to a test data_dir so the REAL resume block reads OUR
    # seeded snapshot (mirrors test_g1_resume_graph._redirect_snapshot_io).
    data_dir = tmp_path / "snap_data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    import systemu.runtime.execution_snapshot as _es
    rw, rr, rd = _es.write_snapshot, _es.read_snapshot, _es.delete_snapshot
    monkeypatch.setattr(_es, "write_snapshot", lambda snap, **kw: rw(snap, data_dir=data_dir))
    monkeypatch.setattr(_es, "read_snapshot", lambda eid, **kw: rr(eid, data_dir=data_dir))
    monkeypatch.setattr(_es, "delete_snapshot", lambda eid, **kw: rd(eid, data_dir=data_dir))

    # DE-FLAKE (option b): pin each slice's stamp to a FIXED constant so "matching
    # stamp ⇒ builder not re-invoked" is proven DETERMINISTICALLY — never at the
    # mercy of wall-clock/mtime granularity or intervening vault IO between this
    # priming survey and the resume survey inside execute(). Two mtime-based stamps
    # (capabilities = newest mtime over tools/, roots = dir mtime) could otherwise
    # shift by a tick and legitimately re-survey a slice, flaking this test even
    # though the resume-cache IS wired. survey_situation reads the stamp fns via the
    # module-level _STAMP_FNS dict (bound at import) + _table_stamp (attr lookup at
    # call time), so we pin BOTH: patch _STAMP_FNS entries and the _table_stamp attr.
    # Pinning BEFORE priming means the seeded prior_stamps and the resume-recomputed
    # stamps read the SAME constant, so matching is guaranteed while the intent
    # (cache fed + unchanged slices reused) is fully preserved.
    _FIXED = {"services": "S", "capabilities": "C", "profile": "P",
              "credentials": "R", "roots": "T"}
    _pinned_stamp_fns = {name: (lambda v, _v=val: _v) for name, val in _FIXED.items()}
    monkeypatch.setattr(_si, "_STAMP_FNS", _pinned_stamp_fns, raising=True)
    monkeypatch.setattr(_si, "_table_stamp", lambda v: "TBL", raising=True)

    # Compute a REAL prior (report, stamps) with the PINNED stamps so every slice
    # stamp provably MATCHES the resume-recomputed stamp → cached slices reused.
    prior_report, prior_stamps = asyncio.run(survey := _si.survey_situation(None, vault=vault))

    exec_id = "exec_ra9_resume"
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_ra9",
        activity_id=activity.id, iteration=1, completed_objective_ids=[],
        situation_report=prior_report.model_dump(),
        situation_stamps=prior_stamps,
    )
    write_snapshot(snap, data_dir=data_dir)

    # Spy every source builder: on the resume survey, unchanged slices must NOT
    # rebuild (they come from the restored cache).
    calls = {"services": 0, "capabilities": 0, "profile": 0,
             "credentials": 0, "roots": 0}
    orig = {name: getattr(_si, f"build_{name}") for name in calls}

    def _spy(name):
        def wrapper(*a, **k):
            calls[name] += 1
            return orig[name](*a, **k)
        return wrapper

    for name in calls:
        monkeypatch.setattr(_si, f"build_{name}", _spy(name))

    result, ctx = _drive_capturing_context(
        cfg := _make_config(tmp_path), vault, shadow, activity, monkeypatch,
        resume_from=exec_id)

    assert ctx is not None
    # The resume-restored cache was fed to the survey → unchanged slices reused,
    # NO builder re-invoked (proves the resume-cache-restore is wired).
    assert calls == {"services": 0, "capabilities": 0, "profile": 0,
                     "credentials": 0, "roots": 0}, (
        f"resume must feed the cache so unchanged slices are reused; got {calls}")
    # The report still carries the service (reused from the cache).
    report = getattr(ctx, "_situation_report", None)
    assert report is not None
    assert "https://mcp.example.com/a" in {
        s.get("name") for s in report.get("services", [])}
