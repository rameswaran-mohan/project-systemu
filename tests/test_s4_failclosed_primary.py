"""S4 (fail-closed external-effect credit) — WAVE 2, PRIMARY credit site.

The invariant under test (shadow_runtime.py ~:6047-6115, the LIVE per-iteration
``completes_objective`` credit):

  An Objective with ``requires_external_verification=True`` is credited ONLY when
  ``_read_external_ok(context, obj_id)`` is True — i.e. a PERSISTED
  ``ExternalEvidence`` whose ``confirmed`` is the bool ``True``. The local
  durable-outcome verifier (``process_completion_claim``) is ADVISORY-ONLY for
  such an objective: a soft-pass alone NEVER credits, and ANY exception in the
  credit path fails CLOSED (not credited) + emits an ``UNVERIFIED_EXTERNAL``
  observation. Non-external objectives credit EXACTLY as before (byte-identical).

These are FAILURE-INJECTION tests written test-first: they drive the REAL credit
block by running ``execute()`` with a succeeding tool that CLAIMS the objective,
and monkeypatching ``process_completion_claim`` to model a soft-pass / a raise.

Harness modelled on tests/test_ra10_runtime_fold.py (the B9 live-credit seam).
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import patch


# ─────────────────────────────────────────────────────────────────────────────
#  Harness
# ─────────────────────────────────────────────────────────────────────────────

def _build_entities_objs(tmp_path, objectives):
    """A vault + shadow + api_tool + a scroll carrying ``objectives`` + an activity.
    Mirrors tests/test_ra10_runtime_fold.py::_build_entities_objs."""
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

    shadow = Shadow(id="shadow_s4", name="S4 Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_s4", name="api_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/api_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_s4", name="S4 Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=objectives)
    vault.save_scroll(scroll)
    activity = Activity(id="act_s4", name="S4 Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_s4"], required_skill_ids=[],
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


def _mk_result(*, success=True, parsed=None):
    from systemu.runtime.tool_sandbox import ToolResult
    return ToolResult(success=success, parsed=parsed or {"ok": True})


def _drive_live_credit(tmp_path, monkeypatch, *, objectives, claim_obj_id,
                       completion_side_effect=None, seed_evidence=None,
                       spy_obs=None):
    """Drive execute() so the LLM issues one succeeding TOOL_CALL that CLAIMS
    ``claim_obj_id``, then a deterministic terminal FAIL. Returns
    ``(runtime, result, context)``.

    - ``objectives``: the scroll objective list (stamp
      ``requires_external_verification`` here).
    - ``completion_side_effect``: monkeypatches module-level
      ``process_completion_claim`` — a callable(**kw) that either returns a
      ``CompletionOutcome`` (model a soft-pass) or raises (model a TLS/timeout).
      If None, the real verifier runs (a verifier=None objective soft-passes).
    - ``seed_evidence``: dict written onto ``context._external_evidence`` before
      the loop credits (via a resolve-spy) — e.g. {"1": {"confirmed": True}}.
    - ``spy_obs``: if a list, every add_observation payload is appended to it.
    """
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_entities_objs(tmp_path, objectives)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    runtime = ShadowRuntime(cfg, vault)

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    if completion_side_effect is not None:
        monkeypatch.setattr(_sr, "process_completion_claim", completion_side_effect)

    # Seed the external-evidence store on the LIVE context the moment it is
    # resolvable (before the credit loop runs). _resolve_objectives_for_run
    # receives context=; it runs once, before the main loop.
    orig_resolve = _sr._resolve_objectives_for_run

    def _resolve_spy(**kw):
        objs, sj = orig_resolve(**kw)
        _ctx = kw.get("context")
        if seed_evidence is not None and _ctx is not None:
            _ctx._external_evidence = dict(seed_evidence)
        _resolve_spy.context = _ctx
        return objs, sj
    _resolve_spy.context = None
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _resolve_spy)

    # Spy observations if asked.
    if spy_obs is not None:
        import systemu.runtime.context_builder as _cb
        _orig_add = _cb.ExecutionContext.add_observation

        def _spy_add(self, obs, ab):
            try:
                spy_obs.append(obs)
            except Exception:
                pass
            return _orig_add(self, obs, ab)
        monkeypatch.setattr(_cb.ExecutionContext, "add_observation", _spy_add)

    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": claim_obj_id, "reasoning": "do the external effect"},
        # Deterministic terminal: reached iff the credit did NOT finalize success.
        {"action": "FAIL", "reason": "reached only if the objective was NOT credited"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(shadow, activity))
    return runtime, result, _resolve_spy.context


def _softpass_outcome(**kw):
    """A process_completion_claim replacement modelling a local SOFT-PASS
    (verifier credits) — the advisory verdict S4 must NOT trust for an external
    objective without confirmed evidence."""
    from systemu.runtime.shadow_runtime import CompletionOutcome, ObjectiveState
    return CompletionOutcome(credited=True, state=ObjectiveState())


def _hardreject_outcome(**kw):
    """A process_completion_claim replacement modelling a local HARD-REJECT
    (verifier explicitly does NOT credit). For an EXTERNAL objective this local
    verdict is ADVISORY-ONLY per §5.8 — it must NOT gate an externally-confirmed
    credit. Emits a feedback_message so the advisory observation trail still fires."""
    from systemu.runtime.shadow_runtime import CompletionOutcome, ObjectiveState
    return CompletionOutcome(
        credited=False, state=ObjectiveState(),
        feedback_message="local verifier could not see the external effect")


def _raise_tls(**kw):
    """A process_completion_claim replacement modelling a TLS/timeout blow-up in
    the credit path (an independent-verify read that throws)."""
    raise RuntimeError("TLS handshake timed out talking to the effect endpoint")


def _external_obj(**overrides):
    from systemu.core.models import Objective
    base = dict(id=1, goal="POST the row to the external API",
                success_criteria="row visible via readback",
                requires_external_verification=True)
    base.update(overrides)
    return Objective(**base)


# ─────────────────────────────────────────────────────────────────────────────
#  Failure-injection tests (S4 gate, PRIMARY site)
# ─────────────────────────────────────────────────────────────────────────────

def test_tls_timeout_in_credit_path_credits_nothing_external(tmp_path, monkeypatch):
    """(1) external objective; the local verifier RAISES (TLS/timeout) in the
    credit path → NOT credited (no status=success) + an UNVERIFIED_EXTERNAL
    observation is emitted. The legacy 'crediting without verify' except must NOT
    fire for an external objective."""
    obs = []
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch,
        objectives=[_external_obj()], claim_obj_id=1,
        completion_side_effect=_raise_tls, spy_obs=obs)

    assert result.get("status") != "success", (
        "an external objective whose credit-path verifier RAISED must NOT reach "
        f"status=success; got {result.get('status')}")
    unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
    assert unv, f"expected an UNVERIFIED_EXTERNAL observation; saw {obs}"
    assert unv[0].get("objective_id") == 1


def test_softpass_verifier_no_external_evidence_does_not_credit(tmp_path, monkeypatch):
    """(2) external objective; the local verifier SOFT-PASSES (credited=True) but
    there is NO external evidence (_external_ok=False) → NOT credited. The
    advisory-only conjunct: a local pass without confirmed evidence never credits."""
    obs = []
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch,
        objectives=[_external_obj()], claim_obj_id=1,
        completion_side_effect=_softpass_outcome, spy_obs=obs)

    assert result.get("status") != "success", (
        "a local soft-pass alone must NOT credit an external objective; "
        f"got {result.get('status')}")
    unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
    assert unv, f"expected an UNVERIFIED_EXTERNAL observation; saw {obs}"


def test_output_type_data_net_mutate_still_requires_confirmed(tmp_path, monkeypatch):
    """(3) external objective with output_type='data' (a net-mutating data effect)
    → not credited without confirmed evidence, even on a soft-pass."""
    obj = _external_obj(output_type="data")
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch,
        objectives=[obj], claim_obj_id=1,
        completion_side_effect=_softpass_outcome)
    assert result.get("status") != "success", (
        "output_type=data external effect must still require confirmed evidence; "
        f"got {result.get('status')}")


def test_unknown_effect_tag_still_requires_confirmed(tmp_path, monkeypatch):
    """(4, BLOCKER-3 AC) external objective from an UNKNOWN effect stamp
    (requires_external_verification=True with an unusual output_type) → not
    credited without confirmed evidence. The stamp — not the tag taxonomy — gates."""
    obj = _external_obj(output_type="mystery_effect")
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch,
        objectives=[obj], claim_obj_id=1,
        completion_side_effect=_softpass_outcome)
    assert result.get("status") != "success", (
        "an unknown-effect external stamp must still fail closed without confirmed "
        f"evidence; got {result.get('status')}")


def test_confirmed_evidence_credits_external(tmp_path, monkeypatch):
    """(5) external objective WITH a persisted ExternalEvidence confirmed=True on
    context._external_evidence → CREDITED (the run finalizes success). Proves the
    gate is not a blanket block — a real confirmed bit clears it."""
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch,
        objectives=[_external_obj()], claim_obj_id=1,
        completion_side_effect=_softpass_outcome,
        seed_evidence={"1": {"objective_id": 1, "confirmed": True}})
    assert result.get("status") == "success", (
        "an external objective with a confirmed ExternalEvidence must credit; "
        f"got {result.get('status')}")


def test_local_hardreject_but_external_confirmed_CREDITS(tmp_path, monkeypatch):
    """(5b, §5.8 authoritative-external) an external objective where the LOCAL
    verifier HARD-REJECTS (process_completion_claim → credited=False) BUT a
    persisted ExternalEvidence.confirmed=True is present → CREDITED.

    The deterministic external evidence is AUTHORITATIVE; the local verifier is
    ADVISORY-ONLY for an external objective (it judges LOCAL StateDelta and cannot
    see the external effect). A false local reject must NOT block a confirmed
    money-move — blocking it would risk a double-submit on retry.

    This FAILS under the old conjunct (_do_credit = _do_credit and _external_ok,
    where a False local verdict blocked the credit) and PASSES after the fix
    (_do_credit = _external_ok). The advisory verifier still RAN (verifier_rejection
    observation is still emitted) — it just does not gate the credit."""
    obs = []
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch,
        objectives=[_external_obj()], claim_obj_id=1,
        completion_side_effect=_hardreject_outcome, spy_obs=obs,
        seed_evidence={"1": {"objective_id": 1, "confirmed": True}})

    assert result.get("status") == "success", (
        "an external objective with a CONFIRMED ExternalEvidence must credit even "
        "when the local (advisory-only) verifier hard-rejected — external evidence "
        f"is authoritative per §5.8; got {result.get('status')}")
    # The advisory verifier still RAN and surfaced its rejection (audit trail) —
    # it just did not gate the authoritative external credit.
    vr = [o for o in obs if isinstance(o, dict) and o.get("type") == "verifier_rejection"]
    assert vr, (
        "the local verifier must still RUN advisory for an external objective and "
        f"surface its rejection observation for the audit trail; saw {obs}")
    # And NO UNVERIFIED_EXTERNAL fired — the credit was authorised.
    unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
    assert not unv, (
        "an authorised external credit must NOT emit UNVERIFIED_EXTERNAL; saw {unv}"
        .format(unv=unv))


def test_non_external_objective_credits_normally(tmp_path, monkeypatch):
    """(6) requires_external_verification=False; a local soft-pass → CREDITED
    exactly as today. Proves S4's scope: the non-external path is byte-identical."""
    from systemu.core.models import Objective
    obj = Objective(id=1, goal="write the local file",
                    success_criteria="file exists",
                    requires_external_verification=False)
    runtime, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch,
        objectives=[obj], claim_obj_id=1,
        completion_side_effect=_softpass_outcome)
    assert result.get("status") == "success", (
        "a non-external objective with a local soft-pass must credit as today; "
        f"got {result.get('status')}")


def test_composition_b9_blocked_and_external_both_gate(tmp_path, monkeypatch):
    """(7, composition) an objective that is BOTH B9-blocked (a missing
    runtime_error credential requirement) AND external stays blocked — neither
    gate bypasses the other. Here the B9 gate fires first (it wraps the credit
    site), so the objective never credits regardless of external evidence.

    We drive a RESUME so the persisted graph carries the B9 missing-req mutation,
    and seed CONFIRMED external evidence — proving the B9 gate is not bypassed by
    a confirmed external bit (defence in depth: both must clear)."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.core.models import Objective, Requirement
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    import systemu.runtime.shadow_runtime as _sr

    # STATIC scroll: obj 1 external + verifier=None (would soft-pass).
    scroll_objs = [_external_obj(id=1, depends_on=[])]
    vault, shadow, activity = _build_entities_objs(tmp_path, scroll_objs)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)
    # Neutralise the ORTHOGONAL resume recredit hook — isolate the LIVE credit site.
    from systemu.runtime.shadow_runtime import CompletionOutcome, ObjectiveState
    monkeypatch.setattr(_sr, "recredit_on_resume",
                        lambda **kw: CompletionOutcome(credited=False, state=ObjectiveState()),
                        raising=False)

    # Persisted graph: obj 1 carries a MISSING runtime_error credential requirement
    # (B9 blocks it) AND is external.
    missing_req = Requirement(kind="credential", schema_path="api_tool",
                              state="missing", source="runtime_error",
                              value_origin="operator",
                              rationale="need a credential for api_tool")
    graph = [Objective(id=1, goal="POST the row to the external API",
                       success_criteria="row visible via readback",
                       requires_external_verification=True, depends_on=[],
                       origin="backchain", requirements=[missing_req])]
    exec_id = "exec_s4_both_gate"
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_s4",
        activity_id=activity.id, iteration=1,
        completed_objective_ids=[], objective_graph=graph,
        next_objective_id=9,
        # CONFIRMED external evidence is present — yet the B9 gate must still block.
        external_evidence={"1": {"objective_id": 1, "confirmed": True}},
    )
    write_snapshot(snap, data_dir=data_dir)

    runtime = ShadowRuntime(cfg, vault)

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    obs = []
    import systemu.runtime.context_builder as _cb
    _orig_add = _cb.ExecutionContext.add_observation

    def _spy_add(self, o, ab):
        obs.append(o)
        return _orig_add(self, o, ab)
    monkeypatch.setattr(_cb.ExecutionContext, "add_observation", _spy_add)

    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "claim it despite the gate"},
        {"action": "FAIL", "reason": "reached iff the credit was blocked"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r = asyncio.run(runtime.execute(shadow, activity, resume_from_execution_id=exec_id))

    assert r.get("status") != "success", (
        "an objective that is BOTH B9-blocked AND external must NOT credit, even "
        f"with confirmed external evidence; got {r.get('status')}")
    # The B9 credential gate fired (it wraps the S4 gate at this site).
    b9 = [o for o in obs if isinstance(o, dict)
          and o.get("type") == "objective_blocked_credential_gate"]
    assert b9, f"expected the B9 credential-gate observation; saw {obs}"


# ─────────────────────────────────────────────────────────────────────────────
#  S4 — the COMPLETE goal-level accept (the THIRD finalization route)
#
#  The per-objective site above fails an uncredited external objective closed and
#  emits UNVERIFIED_EXTERNAL. But COMPLETE is a SEPARATE finalization route: the
#  intent-engine (default ON) accepts a premature COMPLETE on a goal_verifier PASS
#  even while ``len(completed_objectives) < total_objectives``. The goal verifier
#  judges only LOCAL StateDelta and is BLIND to external effects, so a PASS there
#  must NOT finalize status="success" while a pending requires_external_verification
#  objective has no persisted confirmed ExternalEvidence. This mirrors the B9
#  ``_blocked_ids`` gate already at that site.
# ─────────────────────────────────────────────────────────────────────────────

def _drive_complete_accept(tmp_path, monkeypatch, *, objectives,
                           goal_verified=True, seed_evidence=None, spy_obs=None):
    """Drive execute() so the LLM issues a single COMPLETE action while no
    objective is credited, with the goal verifier patched to a chosen verdict.
    Returns ``(runtime, result, context)``.

    The intent-engine is default ON and a plain chat-origin scroll resolves
    adherence='free' (!= 'strict'), so the COMPLETE block runs its goal-level
    accept: with ``goal_verified=True`` the LEGACY behaviour would finalize
    status='success' even though objectives are pending. The S4 gate must reject
    that finalization when a pending external objective lacks confirmed evidence.
    """
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    import systemu.runtime.goal_verifier as _gv

    vault, shadow, activity = _build_entities_objs(tmp_path, objectives)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # Patch the goal verifier to a deterministic verdict (no LLM, no delta race).
    monkeypatch.setattr(
        _gv, "verify_goal",
        lambda **kw: {"verified": bool(goal_verified), "reason": "patched"},
        raising=False)

    runtime = ShadowRuntime(cfg, vault)

    # Seed the external-evidence store on the LIVE context before the loop runs.
    orig_resolve = _sr._resolve_objectives_for_run

    def _resolve_spy(**kw):
        objs, sj = orig_resolve(**kw)
        _ctx = kw.get("context")
        if seed_evidence is not None and _ctx is not None:
            _ctx._external_evidence = dict(seed_evidence)
        _resolve_spy.context = _ctx
        return objs, sj
    _resolve_spy.context = None
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _resolve_spy)

    if spy_obs is not None:
        import systemu.runtime.context_builder as _cb
        _orig_add = _cb.ExecutionContext.add_observation

        def _spy_add(self, obs, ab):
            try:
                spy_obs.append(obs)
            except Exception:
                pass
            return _orig_add(self, obs, ab)
        monkeypatch.setattr(_cb.ExecutionContext, "add_observation", _spy_add)

    decisions = [
        # Premature COMPLETE — no objective was credited. The goal verifier (patched
        # PASS) would otherwise accept this at goal level.
        {"action": "COMPLETE", "summary": "claim the goal is done"},
        # Deterministic terminal: reached iff the COMPLETE was REJECTED (loop continued).
        {"action": "FAIL", "reason": "reached only if COMPLETE was rejected"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(shadow, activity))
    return runtime, result, _resolve_spy.context


def test_complete_goal_accept_rejected_when_external_unconfirmed(tmp_path, monkeypatch):
    """(8, THE BUG) intent-engine ON, a pending requires_external_verification
    objective with NO confirmed evidence, and the goal verifier PATCHED to PASS →
    the COMPLETE must NOT finalize status='success' (it stays incomplete / carded)
    and an UNVERIFIED_EXTERNAL steering observation is emitted.

    FAILS before the fix (goal_verifier PASS finalizes status='success' — the
    goal verifier is blind to the external effect); PASSES after the S4 gate is
    mirrored at the COMPLETE accept."""
    obs = []
    runtime, result, ctx = _drive_complete_accept(
        tmp_path, monkeypatch,
        objectives=[_external_obj()], goal_verified=True, spy_obs=obs)

    assert result.get("status") != "success", (
        "a goal_verifier PASS must NOT finalize the COMPLETE to status='success' "
        "while a pending external-effect objective lacks confirmed evidence — the "
        f"goal verifier is blind to external effects; got {result.get('status')}")
    unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
    assert unv, (
        "the rejected COMPLETE must emit an UNVERIFIED_EXTERNAL steering "
        f"observation so the shadow obtains confirmed evidence; saw {obs}")
    assert unv[0].get("objective_id") == 1


def test_complete_goal_accept_succeeds_when_external_confirmed(tmp_path, monkeypatch):
    """(9) same as (8) but WITH a persisted ExternalEvidence.confirmed=True for the
    pending objective → the S4 gate is clear and the goal-level COMPLETE can
    finalize success. Proves the gate is not a blanket block on external runs — a
    real confirmed bit lets the goal-accept through."""
    runtime, result, ctx = _drive_complete_accept(
        tmp_path, monkeypatch,
        objectives=[_external_obj()], goal_verified=True,
        seed_evidence={"1": {"objective_id": 1, "confirmed": True}})

    assert result.get("status") == "success", (
        "with a confirmed ExternalEvidence for the pending external objective, the "
        "goal-level COMPLETE must be allowed to finalize success; "
        f"got {result.get('status')}")


def test_complete_goal_accept_non_external_unchanged(tmp_path, monkeypatch):
    """(10, byte-identical scope) a run with NO external objective + a goal
    verifier PASS → the COMPLETE finalizes success EXACTLY as before. Proves the
    S4 gate's ``_ext_pending_ids`` is empty for a non-external run, so the legacy
    goal-level accept is untouched."""
    from systemu.core.models import Objective
    obj = Objective(id=1, goal="determine the local answer",
                    success_criteria="answer stated in chat",
                    requires_external_verification=False)
    runtime, result, ctx = _drive_complete_accept(
        tmp_path, monkeypatch,
        objectives=[obj], goal_verified=True)

    assert result.get("status") == "success", (
        "a non-external run with a goal_verifier PASS must finalize the COMPLETE "
        "to success exactly as before (S4 _ext_pending_ids empty); "
        f"got {result.get('status')}")


def test_complete_goal_accept_no_pass_still_rejected(tmp_path, monkeypatch):
    """(11, guard) with the goal verifier returning no-pass (verified=False) and an
    external objective still pending, the COMPLETE is rejected too — the S4 gate
    composes WITH (does not weaken) the existing pending-objectives reject."""
    runtime, result, ctx = _drive_complete_accept(
        tmp_path, monkeypatch,
        objectives=[_external_obj()], goal_verified=False)
    assert result.get("status") != "success", (
        "a premature COMPLETE with no goal-level pass must still be rejected; "
        f"got {result.get('status')}")


# ─────────────────────────────────────────────────────────────────────────────
#  S4 — the STUCK-PARK goal-accept (the FOURTH finalization route)
#
#  The stuck-park path (shadow_runtime.py ~:6350) also finalizes status="success"
#  via the intent-engine (_intent_goal_success) while objectives are pending, and
#  — like the COMPLETE accept — is blind to external effects. It must NOT finalize
#  success for a pending external objective without confirmed evidence.
# ─────────────────────────────────────────────────────────────────────────────

def _drive_stuck_park(tmp_path, monkeypatch, *, objectives, goal_success=True,
                      seed_evidence=None, spy_obs=None):
    """Drive execute() into the stuck-park rail: the LLM keeps issuing failing
    TOOL_CALLs (no objective ever credited) until the no-progress stuck detector
    triggers, with ``_intent_goal_success`` patched to a chosen verdict and the
    auto-coach disabled so the run reaches the goal-accept branch immediately.
    Returns ``(runtime, result, context)``."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_entities_objs(tmp_path, objectives)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    # Disable the self-steer coach so the stuck detector reaches the goal-accept
    # branch on the first trigger instead of burning steers.
    cfg.auto_coach_enabled = False
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # Patch the stuck-park goal-success probe to a deterministic verdict.
    monkeypatch.setattr(
        _sr, "_intent_goal_success",
        lambda **kw: bool(goal_success), raising=False)
    # Headless: the operator stuck prompt returns None → degrade to 'partial' if we
    # ever fall past the goal-accept branch.
    monkeypatch.setattr(ShadowRuntime, "_ask_stuck_or_degrade",
                        lambda self, **kw: None, raising=False)

    runtime = ShadowRuntime(cfg, vault)

    # Every tool call SUCCEEDS but never CLAIMS an objective → no objective is
    # credited → _iters_since_obj_credit climbs to the no-progress threshold and the
    # stuck detector fires. (Succeeding avoids the consecutive-failure circuit
    # breaker, which would finalize 'failure' before the stuck-park branch runs.)
    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    orig_resolve = _sr._resolve_objectives_for_run

    def _resolve_spy(**kw):
        objs, sj = orig_resolve(**kw)
        _ctx = kw.get("context")
        if seed_evidence is not None and _ctx is not None:
            _ctx._external_evidence = dict(seed_evidence)
        _resolve_spy.context = _ctx
        return objs, sj
    _resolve_spy.context = None
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _resolve_spy)

    if spy_obs is not None:
        import systemu.runtime.context_builder as _cb
        _orig_add = _cb.ExecutionContext.add_observation

        def _spy_add(self, obs, ab):
            try:
                spy_obs.append(obs)
            except Exception:
                pass
            return _orig_add(self, obs, ab)
        monkeypatch.setattr(_cb.ExecutionContext, "add_observation", _spy_add)

    # A long run of succeeding-but-not-crediting tool calls (varying params so the
    # deterministic loop-guard does not force-finish first) climbs the no-progress
    # counter to its threshold and trips the stuck detector.
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"n": _i},
         "reasoning": "work the external effect but never claim it credited"}
        for _i in range(40)
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(shadow, activity))
    return runtime, result, _resolve_spy.context


def test_stuck_park_external_unconfirmed_finalizes_partial_not_success(tmp_path, monkeypatch):
    """(12, THE FOURTH ROUTE) the stuck-park goal-accept must NOT finalize
    status='success' for a pending external objective with no confirmed evidence,
    even when _intent_goal_success returns True. It must degrade to 'partial' (the
    honest stuck outcome). FAILS before the fix (status='success'); PASSES after
    the S4 gate is added to the stuck-park accept."""
    runtime, result, ctx = _drive_stuck_park(
        tmp_path, monkeypatch,
        objectives=[_external_obj()], goal_success=True)
    assert result.get("status") != "success", (
        "the stuck-park goal-accept must NOT finalize success for a pending "
        "external objective lacking confirmed evidence — it is blind to external "
        f"effects; got {result.get('status')}")
    assert result.get("status") == "partial", (
        "an uncredited external objective at stuck-park must degrade to 'partial' "
        f"(the safe honest outcome); got {result.get('status')}")


def test_stuck_park_non_external_goal_success_unchanged(tmp_path, monkeypatch):
    """(13, byte-identical scope) a NON-external run at stuck-park with
    _intent_goal_success True → finalizes success exactly as before (the S4
    _ext_pending_ids gate is empty)."""
    from systemu.core.models import Objective
    obj = Objective(id=1, goal="determine the local answer",
                    success_criteria="answer stated in chat",
                    requires_external_verification=False)
    runtime, result, ctx = _drive_stuck_park(
        tmp_path, monkeypatch, objectives=[obj], goal_success=True)
    assert result.get("status") == "success", (
        "a non-external run at stuck-park with a goal-level pass must finalize "
        f"success exactly as before; got {result.get('status')}")
