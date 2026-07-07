"""R-A10 step B9 (AC4): runtime-error-as-requirement fold.

A tool failure that is really a MISSING REQUIREMENT (a 401/403 auth failure, a
422/404 bad-request) must NOT count toward the stuck bound and fail the run. It
folds into a Requirement + a precede-objective (origin="backchain") wired to run
BEFORE the objective whose tool call failed, then SUSPENDS via the INPUT rail so
the operator supplies the credential/decision; on resume the precede is satisfied
and the original objective retries.

Three layers under test:
  * ``http_error_subclass`` (failure_classifier) — 401/403→"auth", 422/404→
    "semantic", 500/malformed→"other"; prefers parsed status_code, regex fallback.
  * ``fold_runtime_error`` (runtime_fold) — the pure fold: insert a backchain
    precede + build the Requirement; idempotent; degrades to None on an
    unresolvable current objective.
  * the shadow_runtime seam — an auth/semantic http_error folds + suspends
    (status suspended_harness_escalation) with the stuck counters EXEMPT, while a
    500 / non-http failure takes the unchanged reflection path and bumps them.

Harness modelled on tests/test_ra10_graph_persist.py.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch


# ═════════════════════════════════════════════════════════════════════════════
# Part 1 — http_error_subclass unit (test 5)
# ═════════════════════════════════════════════════════════════════════════════

def _mk_result(*, success=False, parsed=None, stderr="", error=None):
    from systemu.runtime.tool_sandbox import ToolResult
    return ToolResult(success=success, parsed=parsed or {}, stderr=stderr, error=error)


def _stuck_thresholds_no_progress() -> int:
    """The runtime's no-progress stuck bound (default 5) — the shared source the
    seam's _stuck_trigger reads, so the idempotent-exemption test tracks it."""
    from systemu.runtime.shadow_runtime import _stuck_thresholds
    return _stuck_thresholds()[0]


def test_subclass_parsed_status_code_auth():
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(_mk_result(parsed={"status_code": 401})) == "auth"
    assert http_error_subclass(_mk_result(parsed={"status_code": 403})) == "auth"


def test_subclass_parsed_status_code_semantic():
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(_mk_result(parsed={"status_code": 422})) == "semantic"
    assert http_error_subclass(_mk_result(parsed={"status_code": 404})) == "semantic"


def test_subclass_parsed_status_code_other():
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(_mk_result(parsed={"status_code": 500})) == "other"
    assert http_error_subclass(_mk_result(parsed={"status_code": 503})) == "other"


def test_subclass_prefers_http_status_code_key():
    """Either parsed key (status_code OR http_status_code) is honored."""
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(_mk_result(parsed={"http_status_code": 401})) == "auth"
    assert http_error_subclass(_mk_result(parsed={"http_status_code": 422})) == "semantic"


def test_subclass_regex_fallback_from_stderr():
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(_mk_result(stderr="HTTP 401 Unauthorized")) == "auth"
    assert http_error_subclass(_mk_result(stderr="server returned 403 Forbidden")) == "auth"
    assert http_error_subclass(_mk_result(stderr="422 Unprocessable Entity")) == "semantic"
    assert http_error_subclass(_mk_result(error="Not Found (404)")) == "semantic"
    assert http_error_subclass(_mk_result(stderr="500 Internal Server Error")) == "other"


def test_subclass_prefers_parsed_over_regex():
    """A parsed int status_code wins over an unrelated number in stderr text."""
    from systemu.runtime.failure_classifier import http_error_subclass
    r = _mk_result(parsed={"status_code": 401}, stderr="took 500 ms, retried 422 times")
    assert http_error_subclass(r) == "auth"


def test_subclass_malformed_never_raises():
    from systemu.runtime.failure_classifier import http_error_subclass
    # Non-int parsed status, no scannable code → "other", never raises.
    assert http_error_subclass(_mk_result(parsed={"status_code": "weird"})) == "other"
    assert http_error_subclass(_mk_result(parsed={"status_code": None})) == "other"
    assert http_error_subclass(_mk_result(stderr="nothing numeric here")) == "other"
    assert http_error_subclass(_mk_result()) == "other"
    # A totally junk object (no attrs) still degrades to "other".
    assert http_error_subclass(object()) == "other"


def test_subclass_only_standalone_codes():
    """A bare 4xx embedded in a longer number must not false-positive."""
    from systemu.runtime.failure_classifier import http_error_subclass
    # 40100 is not a status code — no word-boundary match → "other".
    assert http_error_subclass(_mk_result(stderr="id=40100 processed")) == "other"


# ═════════════════════════════════════════════════════════════════════════════
# Part 1b — Fix 3: tightened HTTP-status detection (no spurious fold)
# ═════════════════════════════════════════════════════════════════════════════

def test_subclass_no_spurious_fold_on_path_segment():
    """Fix 3: a 4xx inside a URL path segment (preceded by '/') is NOT a status."""
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(
        _mk_result(stderr="GET /v1/items/401 request failed")) == "other"


def test_subclass_no_spurious_fold_on_line_number():
    """Fix 3: a 4xx that is a source line-number / frame is NOT a status."""
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(
        _mk_result(stderr='File "httpclient.py", line 401')) == "other"
    assert http_error_subclass(
        _mk_result(stderr="Traceback: app.py:404 in handler")) == "other"


def test_subclass_no_spurious_fold_on_bare_id_token():
    """Fix 3: a bare code co-occurring with an http-ish word but NOT anchored to a
    status marker is NOT a status (e.g. a row id)."""
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(
        _mk_result(stderr="row status for id 403")) == "other"


def test_subclass_anchored_status_still_folds():
    """Fix 3: genuinely anchored statuses still classify (regression floor)."""
    from systemu.runtime.failure_classifier import http_error_subclass
    assert http_error_subclass(_mk_result(stderr="HTTP 401 Unauthorized")) == "auth"
    assert http_error_subclass(_mk_result(stderr="status: 403")) == "auth"
    assert http_error_subclass(_mk_result(stderr="response code 422")) == "semantic"
    assert http_error_subclass(_mk_result(error="Not Found (404)")) == "semantic"
    # A structured parsed status_code is always confident.
    assert http_error_subclass(_mk_result(parsed={"status_code": 401})) == "auth"


def test_classify_http_error_low_confidence_on_fuzzy_token():
    """Fix 3: classify_tool_result's http_error rule must NOT fire high-confidence
    on a bare 4xx token in a path/frame — the B9 seam gates the hard suspend on
    high confidence only, so a fuzzy match falls through to the normal path."""
    from systemu.runtime.failure_classifier import classify_tool_result
    # A path-segment 4xx must NOT be classified as a confident http_error.
    c = classify_tool_result(_mk_result(stderr="GET /v1/items/401 request failed"))
    # Either not http_error at all, OR http_error but NOT high confidence.
    assert not (c.category == "http_error" and c.confidence == "high"), c
    # An anchored status IS a confident http_error.
    c2 = classify_tool_result(_mk_result(stderr="HTTP 401 Unauthorized response"))
    assert c2.category == "http_error", c2


# ═════════════════════════════════════════════════════════════════════════════
# Part 2 — fold_runtime_error pure fold (tests 1-partial, 2, 4, 6)
# ═════════════════════════════════════════════════════════════════════════════

def _objs():
    from systemu.core.models import Objective
    return [
        Objective(id=1, goal="root", success_criteria="Done"),
        Objective(id=2, goal="call the API", success_criteria="Got data",
                  depends_on=[1]),
    ]


def test_fold_auth_inserts_credential_precede():
    """AC4 auth fold: a 401 on objective 2's tool → a credential precede inserted
    BEFORE objective 2, origin="backchain", depends_on wiring the precede first."""
    from systemu.runtime.runtime_fold import fold_runtime_error

    objs = _objs()
    fold = fold_runtime_error(
        objectives=objs, current_obj_id=2, sub="auth",
        tool_name="stripe_charge", service_hint="Stripe", next_id=3,
    )
    assert fold is not None
    # A new objective was inserted (2 → 3 total).
    assert len(fold.objectives) == 3
    ids = [o.id for o in fold.objectives]
    assert fold.precede_id in ids
    assert fold.next_id == 4  # allocator bumped past the new precede

    precede = next(o for o in fold.objectives if o.id == fold.precede_id)
    assert precede.origin == "backchain"
    assert "Stripe" in precede.goal or "stripe" in precede.goal.lower()
    # The precede runs BEFORE objective 2: objective 2 now depends on it.
    target = next(o for o in fold.objectives if o.id == 2)
    assert fold.precede_id in target.depends_on
    # The precede inherits obj 2's ORIGINAL upstream dep (1) and slots ahead of 2.
    assert 1 in precede.depends_on
    assert fold.objectives.index(precede) < fold.objectives.index(target)

    # The Requirement is a credential from a runtime_error, state=missing.
    req = fold.requirement
    assert req.kind == "credential"
    assert req.source == "runtime_error"
    assert req.state == "missing"
    assert req.value_origin == "operator"


def test_fold_semantic_inserts_decision_precede():
    """A 422 → a kind="decision" requirement precede (origin="backchain")."""
    from systemu.runtime.runtime_fold import fold_runtime_error

    objs = _objs()
    fold = fold_runtime_error(
        objectives=objs, current_obj_id=2, sub="semantic",
        tool_name="submit_form", service_hint=None, next_id=3,
    )
    assert fold is not None
    precede = next(o for o in fold.objectives if o.id == fold.precede_id)
    assert precede.origin == "backchain"
    assert fold.requirement.kind == "decision"
    assert fold.requirement.source == "runtime_error"
    assert "submit_form" in precede.goal


def test_fold_unresolvable_current_obj_returns_none():
    """test 6 — degrade: an unresolvable current_obj_id → None (normal path)."""
    from systemu.runtime.runtime_fold import fold_runtime_error

    objs = _objs()
    fold = fold_runtime_error(
        objectives=objs, current_obj_id=999, sub="auth",
        tool_name="t", service_hint="X", next_id=3,
    )
    assert fold is None
    # Also None (never raises) on an empty tree / bad sub.
    assert fold_runtime_error(objectives=[], current_obj_id=1, sub="auth",
                              tool_name="t", service_hint="X", next_id=1) is None


def test_fold_idempotent_no_second_credential_precede():
    """test 4 — two consecutive 401s for the same service insert ONE precede."""
    from systemu.runtime.runtime_fold import fold_runtime_error

    objs = _objs()
    first = fold_runtime_error(
        objectives=objs, current_obj_id=2, sub="auth",
        tool_name="stripe_charge", service_hint="Stripe", next_id=3,
    )
    assert first is not None
    assert len(first.objectives) == 3

    # A SECOND fold for the SAME service on the (now-mutated) tree must NOT add a
    # second credential precede (guard on an existing origin=backchain precede for
    # the same schema_path/service). Fix 2: it returns a DISTINCT already_pending
    # sentinel (NOT a bare None) — the tree is UNCHANGED and it names the existing
    # precede id so the seam can still exempt the stuck counters.
    second = fold_runtime_error(
        objectives=first.objectives, current_obj_id=2, sub="auth",
        tool_name="stripe_charge", service_hint="Stripe", next_id=first.next_id,
    )
    assert second is not None, "idempotence must return an already_pending sentinel, not None"
    assert second.already_pending is True
    assert len(second.objectives) == 3, "idempotent no-op must NOT insert a second precede"
    assert second.precede_id == first.precede_id, "sentinel names the existing precede"
    assert second.requirement is None


def test_insert_precede_origin_param_defaults_planner():
    """The reused _insert_precede_objectives keeps origin="planner" by default (B7
    unchanged) and accepts origin="backchain" for B9."""
    from systemu.runtime.open_world_planner import _insert_precede_objectives

    objs = _objs()
    # Default origin → planner (B7 path unchanged).
    out = _insert_precede_objectives(
        objectives=objs,
        precede=[{"precede_before_objective_id": 2, "goal": "auth first",
                  "success_criteria": "authed"}],
        next_id=3,
    )
    new = next(o for o in out if o.id == 3)
    assert new.origin == "planner"

    # Explicit backchain origin (B9 path).
    out2 = _insert_precede_objectives(
        objectives=objs,
        precede=[{"precede_before_objective_id": 2, "goal": "auth first",
                  "success_criteria": "authed"}],
        next_id=3, origin="backchain",
    )
    new2 = next(o for o in out2 if o.id == 3)
    assert new2.origin == "backchain"


# ═════════════════════════════════════════════════════════════════════════════
# Part 3 — the shadow_runtime seam (tests 1 full, 3)
# ═════════════════════════════════════════════════════════════════════════════

def _build_entities(tmp_path):
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

    shadow = Shadow(id="shadow_b9", name="B9 Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_b9", name="api_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/api_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_b9", name="B9 Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=[
                        Objective(id=1, goal="call the api",
                                  success_criteria="got data")])
    vault.save_scroll(scroll)
    activity = Activity(id="act_b9", name="B9 Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_b9"], required_skill_ids=[],
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


def _run_with_failing_tool(tmp_path, monkeypatch, *, fail_result, decisions,
                           preseed=None):
    """Drive execute() so the first decision is a TOOL_CALL whose _handle_tool_call
    returns ``fail_result``. Returns (runtime, result). ``preseed`` (if given) is
    called with the constructed runtime BEFORE execute() to set stuck counters."""
    from sharing_on.config import Config
    import systemu.runtime.shadow_runtime as _sr

    vault, shadow, activity = _build_entities(tmp_path)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")

    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    from systemu.runtime.shadow_runtime import ShadowRuntime
    runtime = ShadowRuntime(cfg, vault)

    async def _fake_handle(decision, tools, context, current_ab, dry_run, **kw):
        # Mirror the real _handle_tool_call's per-tool consec bump so the seam's
        # depth-exemption (which pops _consec_tool_fails[tool]) is observable.
        if not fail_result.success:
            tn = decision.get("tool_name", "") or "?"
            runtime._consec_tool_fails[tn] = runtime._consec_tool_fails.get(tn, 0) + 1
        return fail_result

    monkeypatch.setattr(runtime, "_handle_tool_call", _fake_handle)
    if preseed is not None:
        preseed(runtime)

    # Make surface_harness_request a no-op so the suspend rail doesn't need a queue.
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # Capture the live ExecutionContext (for the graph-persist assertion).
    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        return objs, sj

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    import asyncio
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(shadow, activity))
    return runtime, result, captured.get("context")


def test_seam_auth_401_folds_and_suspends_with_depth_exemption(tmp_path, monkeypatch):
    """test 1 (AC4): a 401 http_error tool failure folds a credential precede,
    SUSPENDS (suspended_harness_escalation, NOT a terminal failure), persists the
    graph, and EXEMPTS the stuck counters (not bumped by this failure)."""
    fail = _mk_result(success=False, parsed={"status_code": 401},
                      stderr="HTTP 401 Unauthorized", error="401 Unauthorized")
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "hit the api"},
        # A fallback decision in case the loop does NOT suspend (it should).
        {"action": "FAIL", "reason": "should not reach here"},
    ]

    # Pre-seed the stuck counters NON-zero, so the depth-exemption's RESET is a real
    # observable effect (not a vacuous 0==0).
    def _preseed(rt):
        rt._iters_since_obj_credit = 5
        rt._same_tool_fail_streak["api_tool"] = 4
        rt._consec_tool_fails["api_tool"] = 4

    runtime, result, ctx = _run_with_failing_tool(
        tmp_path, monkeypatch, fail_result=fail, decisions=decisions,
        preseed=_preseed)

    assert result.get("status") == "suspended_harness_escalation", result.get("status")

    # Depth-exemption: the fold RESET the stuck counters (a discovered requirement is
    # not lack-of-progress) — none were left bumped by this failure.
    assert runtime._iters_since_obj_credit == 0
    assert runtime._same_tool_fail_streak.get("api_tool", 0) == 0
    assert runtime._consec_tool_fails.get("api_tool", 0) == 0
    # The stuck bound is NOT tripped.
    tripped, _reason = runtime._stuck_trigger()
    assert tripped is False

    # The mutated objective graph was PERSISTED (B5) so a resume rehydrates the
    # inserted backchain precede.
    graph = getattr(ctx, "_objective_graph", None)
    assert graph, "fold must persist context._objective_graph"
    origins = [o.get("origin") for o in graph]
    assert "backchain" in origins, origins
    # The id-allocator floor advanced past the inserted precede.
    assert getattr(ctx, "_next_objective_id", 0) >= 3


def test_seam_semantic_422_folds_and_suspends(tmp_path, monkeypatch):
    """test 2 (seam): a 422 folds a decision precede and suspends."""
    fail = _mk_result(success=False, parsed={"status_code": 422},
                      stderr="HTTP 422 Unprocessable Entity",
                      error="422 bad request")
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "hit the api"},
        {"action": "FAIL", "reason": "should not reach here"},
    ]
    def _preseed(rt):
        rt._iters_since_obj_credit = 3
        rt._same_tool_fail_streak["api_tool"] = 2
        rt._consec_tool_fails["api_tool"] = 2

    runtime, result, ctx = _run_with_failing_tool(
        tmp_path, monkeypatch, fail_result=fail, decisions=decisions,
        preseed=_preseed)
    assert result.get("status") == "suspended_harness_escalation", result.get("status")
    assert runtime._iters_since_obj_credit == 0
    assert runtime._same_tool_fail_streak.get("api_tool", 0) == 0
    assert runtime._consec_tool_fails.get("api_tool", 0) == 0
    # The semantic fold inserted a decision-kind requirement on the backchain precede.
    graph = getattr(ctx, "_objective_graph", None)
    assert graph and "backchain" in [o.get("origin") for o in graph], graph


def test_seam_500_takes_reflection_path_and_bumps_counters(tmp_path, monkeypatch):
    """test 3a: a 500 (http_error but sub="other") does NOT fold — the normal
    reflection path runs and the stuck counters increment normally."""
    fail = _mk_result(success=False, parsed={"status_code": 500},
                      stderr="HTTP 500 Internal Server Error",
                      error="500 server error")
    # After the failure the loop continues; give it a FAIL to terminate cleanly.
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "hit the api"},
        {"action": "FAIL", "reason": "give up"},
    ]
    runtime, result, ctx = _run_with_failing_tool(
        tmp_path, monkeypatch, fail_result=fail, decisions=decisions)

    # NOT suspended by a fold — the run ran its normal course and ended non-suspend.
    assert result.get("status") != "suspended_harness_escalation", result.get("status")
    # The loop-level stuck counters were bumped by the (normal, non-folded) failure
    # — i.e. the depth-exemption did NOT fire. (_consec_tool_fails lives inside the
    # real _handle_tool_call, which the fake bypasses; assert the loop counters that
    # _update_stuck_counters drives + that the fold's exemption would have reset.)
    assert runtime._same_tool_fail_streak.get("api_tool", 0) >= 1
    assert runtime._iters_since_obj_credit >= 1


def test_seam_non_http_failure_takes_reflection_path(tmp_path, monkeypatch):
    """test 3b: a non-http failure (timeout) does NOT fold — normal path, counters bump."""
    fail = _mk_result(success=False, stderr="operation timed out after 30s",
                      error="timeout")
    decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "hit the api"},
        {"action": "FAIL", "reason": "give up"},
    ]
    runtime, result, ctx = _run_with_failing_tool(
        tmp_path, monkeypatch, fail_result=fail, decisions=decisions)
    assert result.get("status") != "suspended_harness_escalation", result.get("status")
    # A non-http failure is UNAFFECTED by the fold — the loop-level stuck counter bumps.
    assert runtime._same_tool_fail_streak.get("api_tool", 0) >= 1
    assert runtime._iters_since_obj_credit >= 1


# ═════════════════════════════════════════════════════════════════════════════
# Part 4 — Fix 1: the fold's INPUT request carries the re-dispatch rail
# ═════════════════════════════════════════════════════════════════════════════

def test_fold_input_request_auth_carries_pending_tool_and_secret():
    """Fix 1: the auth fold's INPUT request must carry ``pending_tool`` (the failed
    tool + original params) and a ``runtime_fold`` marker so the resume rail
    RE-DISPATCHES the tool with the operator credential merged (URL-mode secret).

    Previously the spec had NO pending_tool → on resume only an advisory
    observation was injected and the credential never reached the tool."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from sharing_on.config import Config
    from systemu.core.models import Requirement

    rt = ShadowRuntime.__new__(ShadowRuntime)   # no full init needed for this pure builder
    req = Requirement(kind="credential", schema_path="Stripe", state="missing",
                      source="runtime_error", value_origin="operator",
                      rationale="need a Stripe key")
    r = rt._build_runtime_fold_input_request(
        sub="auth", tool_name="stripe_charge", requirement=req,
        pending_params={"amount": 100, "currency": "usd"},
        precede_id=7,
    )
    spec = r.spec
    # The pending tool + its ORIGINAL params ride along so resume can re-dispatch.
    assert spec.get("pending_tool"), "auth fold must carry pending_tool for re-dispatch"
    assert spec["pending_tool"]["tool_name"] == "stripe_charge"
    assert spec["pending_tool"]["parameters"] == {"amount": 100, "currency": "usd"}
    # The credential field name is a SECRET (URL-mode) — never a typed form input.
    assert "credential" in (spec.get("secret_fields") or [])
    # Fold markers survive so the resume site can satisfy+credit the precede.
    assert spec.get("runtime_fold") is True
    assert spec.get("requirement_kind") == "credential"
    assert spec.get("requirement_schema_path") == "Stripe"
    assert spec.get("precede_id") == 7


def _stamp_grant_note(data_dir, exec_id, grant_payload):
    """Read the CYCLE1 fold snapshot, append a __HARNESS_GRANT__ note carrying the
    operator's resolved grant_payload, and re-write it (what resume_after_grant
    does in production)."""
    import json as _json
    from systemu.runtime.execution_snapshot import read_snapshot, write_snapshot
    snap = read_snapshot(exec_id, data_dir=data_dir)
    assert snap is not None, "CYCLE1 fold must have written a resumable snapshot"
    snap.sticky_notes.append(
        f"__HARNESS_GRANT__::{exec_id}::" + _json.dumps(grant_payload))
    write_snapshot(snap, data_dir=data_dir)
    return snap


def test_resume_round_trip_credits_precede_and_redispatches(tmp_path, monkeypatch):
    """Fix 1 END-TO-END: CYCLE1 401 → fold+suspend; stamp the operator credential;
    CYCLE2 resume → (a) the backchain precede's Requirement is satisfied and its id
    is credited into completed_objectives, (b) the credential reaches the RE-DISPATCHED
    tool, (c) the original objective retries (not blocked forever, not re-folding)."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    import asyncio

    vault, shadow, activity = _build_entities(tmp_path)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # ── CYCLE 1: 401 fold + suspend ─────────────────────────────────────────
    fail = _mk_result(success=False, parsed={"status_code": 401},
                      stderr="HTTP 401 Unauthorized", error="401 Unauthorized")
    runtime = ShadowRuntime(cfg, vault)

    async def _fail_handle(decision, tools, context, current_ab, dry_run, **kw):
        tn = decision.get("tool_name", "") or "?"
        runtime._consec_tool_fails[tn] = runtime._consec_tool_fails.get(tn, 0) + 1
        return fail
    monkeypatch.setattr(runtime, "_handle_tool_call", _fail_handle)

    exec_id_holder = {}
    orig_capture = _sr.capture_from_context if hasattr(_sr, "capture_from_context") else None

    c1_decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"q": "x"},
         "completes_objective": 1, "reasoning": "hit the api"},
        {"action": "FAIL", "reason": "should not reach"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c1_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r1 = asyncio.run(runtime.execute(shadow, activity))
    assert r1.get("status") == "suspended_harness_escalation", r1.get("status")
    exec_id = r1.get("execution_id")
    assert exec_id, r1

    # The fold's INPUT request (persisted in the __HARNESS_PENDING__ note) must carry
    # pending_tool + the runtime_fold markers.
    import json as _json
    snap1 = _sr.read_snapshot(exec_id, data_dir=data_dir) if hasattr(_sr, "read_snapshot") else None
    from systemu.runtime.execution_snapshot import read_snapshot as _rs
    snap1 = _rs(exec_id, data_dir=data_dir)
    _pend = next((n for n in snap1.sticky_notes if n.startswith("__HARNESS_PENDING__::")), None)
    assert _pend, "fold must persist a __HARNESS_PENDING__ note"
    _pend_spec = _json.loads(_pend.split("::", 2)[2])["spec"]
    assert _pend_spec.get("pending_tool", {}).get("tool_name") == "api_tool"
    assert _pend_spec.get("runtime_fold") is True
    _precede_id = _pend_spec.get("precede_id")
    assert isinstance(_precede_id, int), _pend_spec

    # ── Operator supplies the credential (URL-mode secret → out-of-band). The
    #    reconciler builds a grant_payload with pending_tool + param_answers. For an
    #    all-secret credential param_answers is EMPTY but the runtime_fold marker
    #    tells the resume rail to re-dispatch anyway (secret is in the store/env). ──
    grant_payload = {
        "kind": "input",
        "param_answers": {},                 # secret went URL-mode, not through the form
        "requested_schema": _pend_spec.get("requested_schema") or {},
        "pending_tool": _pend_spec.get("pending_tool"),
        "runtime_fold": True,
        "requirement_kind": "credential",
        "requirement_schema_path": "api_tool",
        "precede_id": _precede_id,
    }
    _stamp_grant_note(data_dir, exec_id, grant_payload)

    # ── CYCLE 2: resume. Now the tool SUCCEEDS (credential present) and the
    #    re-dispatch is observed. ──
    redispatched = {}

    async def _ok_handle(decision, tools, context, current_ab, dry_run, **kw):
        redispatched["decision"] = decision
        redispatched["count"] = redispatched.get("count", 0) + 1
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _ok_handle)

    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["objectives"] = objs
        captured["context"] = kw.get("context")
        return objs, sj
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    # On resume the original objective (id=1) retries its tool call; then completes.
    c2_decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"q": "x"},
         "completes_objective": 1, "reasoning": "retry with credential"},
        {"action": "FINISH", "reason": "done"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c2_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r2 = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    objs = captured.get("objectives")
    assert objs is not None, "resume did not reach the objectives rebuild"
    assert _precede_id in [o.id for o in objs], [o.id for o in objs]

    # (a) The backchain precede's Requirement was SATISFIED — read the re-persisted
    # durable graph on the context (the fold-credit rewrites context._objective_graph
    # after flipping the requirement, so this reflects the post-credit state).
    ctx = captured.get("context")
    graph = getattr(ctx, "_objective_graph", None)
    assert graph, "resume must re-persist the satisfied graph"
    g_precede = next((o for o in graph if o.get("id") == _precede_id), None)
    assert g_precede is not None, graph
    assert g_precede.get("origin") == "backchain"
    _greqs = g_precede.get("requirements") or []
    assert _greqs, "precede should carry the folded requirement"
    # Requirement.state has no "satisfied" literal — the terminal/bound state is "have".
    assert _greqs[0].get("state") == "have", _greqs[0]

    # (a cont.) The precede id was CREDITED into completed_objectives so the original
    # objective's depends_on gate opens and it retries.
    assert _precede_id in getattr(runtime, "_resume_completed_precedes", set()), \
        getattr(runtime, "_resume_completed_precedes", None)
    # The run did NOT re-fold / re-suspend and did NOT block forever.
    assert r2.get("status") != "suspended_harness_escalation", r2.get("status")

    # (b) The credential reached the RE-DISPATCHED tool: the original tool call ran
    # again on resume (the re-dispatch closure OR the loop's retry).
    assert redispatched.get("count", 0) >= 1, "tool must re-dispatch/retry on resume"
    assert redispatched["decision"].get("tool_name") == "api_tool"


# ═════════════════════════════════════════════════════════════════════════════
# Part 5 — Fix 2: the depth-exemption survives the idempotent no-op
# ═════════════════════════════════════════════════════════════════════════════

def test_idempotent_pending_401_stays_exempt(tmp_path, monkeypatch):
    """Fix 2: two 401s on the SAME service across the loop. The FIRST folds+suspends.
    A SECOND 401 (idempotent — the precede is already pending + still missing) must
    NOT bump _iters_since_obj_credit / _same_tool_fail_streak / loop_guard and must
    NOT trip the stuck bound. It re-suspends/parks — it never fails-for-no-progress.

    Modelled by pre-seeding a graph that ALREADY carries the backchain precede (so
    fold_runtime_error returns the idempotent-pending sentinel) and driving one more
    401 through the seam."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    from systemu.core.models import Objective, Requirement
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    import asyncio

    vault, shadow, activity = _build_entities(tmp_path)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # Seed a resume snapshot whose graph ALREADY has the backchain credential precede
    # for "api_tool" (as if CYCLE1 folded it). A repeated 401 must be idempotent.
    exec_id = "exec_idem"
    req = Requirement(kind="credential", schema_path="api_tool", state="missing",
                      source="runtime_error", value_origin="operator")
    graph = [
        Objective(id=2, goal="Obtain credential for api_tool",
                  success_criteria="cred available", depends_on=[],
                  origin="backchain", requirements=[req]),
        Objective(id=1, goal="call the api", success_criteria="got data",
                  depends_on=[2]),
    ]
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b9",
        activity_id=activity.id, iteration=1, completed_objective_ids=[],
        objective_graph=graph, next_objective_id=3,
    )
    write_snapshot(snap, data_dir=data_dir)

    runtime = ShadowRuntime(cfg, vault)
    fail = _mk_result(success=False, parsed={"status_code": 401},
                      stderr="HTTP 401 Unauthorized", error="401 Unauthorized")

    async def _fail_handle(decision, tools, context, current_ab, dry_run, **kw):
        tn = decision.get("tool_name", "") or "?"
        runtime._consec_tool_fails[tn] = runtime._consec_tool_fails.get(tn, 0) + 1
        return fail
    monkeypatch.setattr(runtime, "_handle_tool_call", _fail_handle)

    # Pre-seed stuck counters NON-zero so the idempotent-exemption reset is observable.
    runtime._iters_since_obj_credit = 4
    runtime._same_tool_fail_streak["api_tool"] = 3

    # Drive: on resume, the credential precede (id=2) is the current objective. Its
    # tool call 401s AGAIN — SEVERAL times. Each idempotent-pending 401 must be
    # exempt (never bumping the stuck counters). With the no-progress bound normally
    # ~5, this many repeated 401s would trip it and FAIL the run for no-progress
    # WITHOUT the exemption. A terminal FAIL then ends the drive cleanly.
    stuck_no_progress = _stuck_thresholds_no_progress()
    n_401 = stuck_no_progress + 3

    _seq = ([
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 2, "reasoning": "retry cred obtain"}
    ] * n_401) + [{"action": "FAIL", "reason": "parked on still-pending precede"}]
    _it = iter(_seq)

    def _next_decision(*a, **k):
        try:
            return next(_it)
        except StopIteration:
            return {"action": "FAIL", "reason": "parked on still-pending precede"}

    # Spy on _stuck_trigger — it must NEVER report tripped across the whole run
    # (without the exemption, the accumulated 401s WOULD trip the no-progress bound).
    tripped_seen = {"v": False}
    _orig_trigger = runtime._stuck_trigger

    def _spy_trigger():
        t, r = _orig_trigger()
        if t:
            tripped_seen["v"] = True
        return t, r
    monkeypatch.setattr(runtime, "_stuck_trigger", _spy_trigger)

    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=_next_decision), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        result = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    # Each idempotent-pending 401 was exempt — the same-tool-fail streak never
    # accumulated (it is reset every iteration by the exemption).
    assert runtime._same_tool_fail_streak.get("api_tool", 0) == 0, \
        "idempotent-pending 401s must NOT accumulate a same-tool-fail streak"
    # The no-progress counter never reached the stuck bound (each 401 reset it; the
    # only non-401 iteration is the terminal FAIL, which bumps it by at most 1).
    assert runtime._iters_since_obj_credit < stuck_no_progress, \
        (runtime._iters_since_obj_credit, stuck_no_progress)
    # The stuck bound was NEVER tripped across the run.
    assert tripped_seen["v"] is False, "idempotent-pending 401s must never trip the stuck bound"
    # And the run did NOT fail-for-no-progress (it ended on the explicit terminal FAIL).
    assert "no objective credit" not in str(result.get("final_summary", "")).lower()


# ═════════════════════════════════════════════════════════════════════════════
# Part 6 — Regression fixes (re-broaden anchored HTTP-status + guard schema None
#          + bound wrong-credential precede growth)
# ═════════════════════════════════════════════════════════════════════════════

# ── Fix 1: the anchored pattern MUST recognize the real requests/marker shapes ──
# These are the canonical Python `requests` `raise_for_status()` string plus the
# common free-text marker forms. The prior tightening OVER-NARROWED and missed all
# of them (they folded pre-b0164472, then regressed to `unknown`/`other`).
_REAL_AUTH_SHAPES = [
    "401 Client Error: Unauthorized for url: https://api.stripe.com/v1/charges",
    "403 Client Error: Forbidden",
    "Server responded with 401",
    "Bearer token rejected: 401",
    "Request failed with 401",
    "Got 401 back",
]
_REAL_SEMANTIC_SHAPES = [
    "422 Client Error: Unprocessable Entity",
    "404 Client Error: Not Found",
]


@pytest.mark.parametrize("shape", _REAL_AUTH_SHAPES)
def test_fix1_real_auth_shapes_fold(shape):
    """Fix 1: the real requests/marker 401/403 shapes classify as a CONFIDENT
    http_error AND sub-classify as "auth" (so the seam folds a credential precede)."""
    from systemu.runtime.failure_classifier import (
        classify_tool_result, http_error_subclass,
    )
    r = _mk_result(error=shape)
    assert http_error_subclass(r) == "auth", (shape, http_error_subclass(r))
    assert classify_tool_result(r).category == "http_error", (shape,)


@pytest.mark.parametrize("shape", _REAL_SEMANTIC_SHAPES)
def test_fix1_real_semantic_shapes_fold(shape):
    """Fix 1: the real requests 422/404 shapes → http_error + "semantic"."""
    from systemu.runtime.failure_classifier import (
        classify_tool_result, http_error_subclass,
    )
    r = _mk_result(error=shape)
    assert http_error_subclass(r) == "semantic", (shape, http_error_subclass(r))
    assert classify_tool_result(r).category == "http_error", (shape,)


def test_fix1_500_server_error_shape_is_http_other():
    """Fix 1: `"500 Server Error"` (requests 5xx shape) is a CONFIDENT http_error
    but sub="other" (a transient server fault → normal reflection path, no fold)."""
    from systemu.runtime.failure_classifier import (
        classify_tool_result, http_error_subclass,
    )
    r = _mk_result(error="500 Server Error")
    assert classify_tool_result(r).category == "http_error"
    assert http_error_subclass(r) == "other"


def test_fix1_parsed_status_code_still_folds():
    """Fix 1: a structured parsed status_code path is unaffected (still folds)."""
    from systemu.runtime.failure_classifier import (
        classify_tool_result, http_error_subclass,
    )
    r = _mk_result(parsed={"status_code": 401})
    assert http_error_subclass(r) == "auth"
    assert classify_tool_result(r).category == "http_error"


def test_fix1_bundled_api_call_get_shape_folds():
    """Fix 1 (ground-truth): the project's OWN bundled tool returns exactly the
    requests `str(exc)` shape via `response.raise_for_status()`; it MUST fold."""
    from systemu.runtime.failure_classifier import (
        classify_tool_result, http_error_subclass,
    )
    # What api_call_get.py returns: {"success": False, "error": str(exc)}.
    r = _mk_result(
        error="401 Client Error: Unauthorized for url: https://api.stripe.com/v1/charges")
    assert http_error_subclass(r) == "auth"
    assert classify_tool_result(r).category == "http_error"


# ── Fix 1: the spurious token shapes MUST STAY excluded (no re-admission) ──
_SPURIOUS_SHAPES = [
    "GET /v1/items/401 request failed",
    'File "httpclient.py", line 401',
    "app.py:404",
    "error 40100",
    "row status for id 403",
]


@pytest.mark.parametrize("shape", _SPURIOUS_SHAPES)
def test_fix1_spurious_shapes_still_excluded(shape):
    """Fix 1: path segments, source frames, longer numbers, and a non-adjacent
    marker/code must NOT fold — http_error_subclass→"other" (never auth/semantic)
    and classify_tool_result must NOT be a confident http_error."""
    from systemu.runtime.failure_classifier import (
        classify_tool_result, http_error_subclass, _anchored_http_status,
    )
    assert _anchored_http_status(shape) is None, (shape,)
    assert http_error_subclass(_mk_result(error=shape)) == "other", (shape,)
    c = classify_tool_result(_mk_result(error=shape))
    assert c.category != "http_error", (shape, c.category)


# ── Fix 2: schema_path=None credit must NOT flip ALL runtime_error requirements ──

def test_fix2_none_schema_credit_flips_only_matching_kind():
    """Fix 2: a precede carrying TWO runtime_error requirements (a credential + a
    decision) + a resume-fold-credit whose requirement_schema_path is None must flip
    ONLY the requirement matching the fold's requirement_kind — NOT flip-all."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.core.models import Objective, Requirement

    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt._resume_completed_precedes = set()
    # The stashed credit has schema_path=None but names the fold's kind (credential).
    rt._resume_fold_credit = {
        "precede_id": 2,
        "requirement_kind": "credential",
        "requirement_schema_path": None,
        "operator_value": None,   # credential → URL-mode secret, no form answers
    }

    cred = Requirement(kind="credential", schema_path="Stripe", state="missing",
                       source="runtime_error", value_origin="operator")
    dec = Requirement(kind="decision", schema_path="submit_form", state="missing",
                      source="runtime_error", value_origin="operator")
    precede = Objective(id=2, goal="obtain credential", success_criteria="cred",
                        depends_on=[], origin="backchain",
                        requirements=[cred, dec])
    orig = Objective(id=1, goal="call api", success_criteria="data",
                     depends_on=[2])
    objs = [precede, orig]

    class _Ctx:
        _objective_graph = None
    completed = set()

    out = rt._apply_resume_fold_credit(
        objectives=objs, completed_objectives=completed, context=_Ctx())

    new_precede = next(o for o in out if o.id == 2)
    by_kind = {r.kind: r for r in new_precede.requirements}
    assert by_kind["credential"].state == "have", "matching-kind req must flip"
    assert by_kind["decision"].state == "missing", \
        "the OTHER runtime_error requirement must NOT be flip-all'd to have"


def test_fix2_none_schema_with_single_req_still_flips():
    """Fix 2: the common single-requirement-per-precede case is unaffected — a
    None schema_path still flips the one runtime_error requirement (kind-matched)."""
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.core.models import Objective, Requirement

    rt = ShadowRuntime.__new__(ShadowRuntime)
    rt._resume_completed_precedes = set()
    rt._resume_fold_credit = {
        "precede_id": 2,
        "requirement_kind": "credential",
        "requirement_schema_path": None,
        "operator_value": None,
    }
    cred = Requirement(kind="credential", schema_path="Stripe", state="missing",
                       source="runtime_error", value_origin="operator")
    precede = Objective(id=2, goal="obtain credential", success_criteria="cred",
                        depends_on=[], origin="backchain", requirements=[cred])
    orig = Objective(id=1, goal="call api", success_criteria="data", depends_on=[2])

    class _Ctx:
        _objective_graph = None
    completed = set()
    out = rt._apply_resume_fold_credit(
        objectives=[precede, orig], completed_objectives=completed, context=_Ctx())
    new_precede = next(o for o in out if o.id == 2)
    assert new_precede.requirements[0].state == "have"
    assert 2 in completed


# ── Fix 3: bound the wrong-credential precede growth ──

def test_fix3_wrong_credential_reuses_existing_precede():
    """Fix 3: a re-401 for a service that ALREADY has a backchain precede whose
    requirement is state="have" (a wrong credential was tried) must REUSE that
    precede — flip it back to "missing" — instead of inserting a NEW precede. The
    graph does NOT grow a second backchain precede across wrong-credential cycles."""
    from systemu.runtime.runtime_fold import fold_runtime_error
    from systemu.core.models import Objective, Requirement

    # A tree that already carries a CREDITED (state="have") backchain precede for
    # "Stripe" — i.e. the operator supplied a credential that then re-401'd.
    have_req = Requirement(kind="credential", schema_path="Stripe", state="have",
                           source="runtime_error", value_origin="operator",
                           bound_value_ref="operator:credential")
    objs = [
        Objective(id=1, goal="root", success_criteria="done"),
        Objective(id=3, goal="Obtain credential for Stripe",
                  success_criteria="cred", depends_on=[1], origin="backchain",
                  requirements=[have_req]),
        Objective(id=2, goal="call the API", success_criteria="data",
                  depends_on=[1, 3]),
    ]

    before_backchain = [o for o in objs if o.origin == "backchain"]
    assert len(before_backchain) == 1

    fold = fold_runtime_error(
        objectives=objs, current_obj_id=2, sub="auth",
        tool_name="stripe_charge", service_hint="Stripe", next_id=4,
    )
    assert fold is not None, "a re-401 on a have-state precede must be handled"
    # No NEW precede inserted — the existing one is REUSED.
    after_backchain = [o for o in fold.objectives if o.origin == "backchain"]
    assert len(after_backchain) == 1, \
        "wrong-credential re-401 must NOT insert a second backchain precede"
    # The reused precede id is named (== the existing precede's id, 3).
    assert fold.precede_id == 3, fold.precede_id
    # Its requirement was flipped BACK to "missing" so the operator is re-asked.
    reused = next(o for o in fold.objectives if o.id == 3)
    reused_req = reused.requirements[0]
    assert reused_req.state == "missing", "requirement must be re-asked (missing)"


def test_fix3_wrong_credential_two_cycles_no_growth():
    """Fix 3: two wrong-credential cycles on the same service → still ONE backchain
    precede (no +1 growth per cycle)."""
    from systemu.runtime.runtime_fold import fold_runtime_error
    from systemu.core.models import Objective, Requirement

    have_req = Requirement(kind="credential", schema_path="Stripe", state="have",
                           source="runtime_error", value_origin="operator")
    objs = [
        Objective(id=1, goal="root", success_criteria="done"),
        Objective(id=3, goal="Obtain credential for Stripe",
                  success_criteria="cred", depends_on=[1], origin="backchain",
                  requirements=[have_req]),
        Objective(id=2, goal="call the API", success_criteria="data",
                  depends_on=[1, 3]),
    ]
    # Cycle 1: re-401 → reuse + flip back to missing.
    f1 = fold_runtime_error(objectives=objs, current_obj_id=2, sub="auth",
                            tool_name="stripe_charge", service_hint="Stripe",
                            next_id=4)
    assert f1 is not None
    assert len([o for o in f1.objectives if o.origin == "backchain"]) == 1
    # Now the precede is missing again → a further identical 401 is idempotent-pending
    # (already covered by the existing missing precede) — still no growth.
    f2 = fold_runtime_error(objectives=f1.objectives, current_obj_id=2, sub="auth",
                            tool_name="stripe_charge", service_hint="Stripe",
                            next_id=f1.next_id)
    assert f2 is not None
    assert len([o for o in f2.objectives if o.origin == "backchain"]) == 1, \
        "second cycle must not grow a third precede"


# ═════════════════════════════════════════════════════════════════════════════
# Part 7 — Fix A (HIGH, safety): un-credit the precede on a wrong-credential
#          re-ask so the ORIGINAL objective's depends_on gate RE-CLOSES.
#
# The prior fix (d43f7aef) added the wrong-credential "reuse" path: it flips a
# satisfied precede's Requirement state "have"→"missing" and re-suspends. But it
# NEVER removes the precede id from completed_objectives (credited on the resume),
# so the scheduling gate reports the ORIGINAL objective (depends_on=[precede]) as
# READY with the credential missing → the LLM can COMPLETE it unauthenticated via
# any other succeeding action. Fix A makes the reuse path SIGNAL the re-ask on the
# FoldResult (reask_precede_id) so the seam discards that id from EVERY set that
# tracks precede completion BEFORE re-suspending — re-closing the gate.
# ═════════════════════════════════════════════════════════════════════════════

def test_fixA_reuse_path_signals_reask_on_foldresult():
    """Fix A (unit): the wrong-credential reuse branch of fold_runtime_error must
    SIGNAL the re-ask on the FoldResult (reask_precede_id names the reused precede)
    so the seam knows to un-credit it. The idempotent-pending / fresh-insert paths
    must NOT set it (their precede was never credited into completed_objectives)."""
    from systemu.runtime.runtime_fold import fold_runtime_error
    from systemu.core.models import Objective, Requirement

    # A satisfied (state="have") backchain precede for "Stripe" → the reuse path.
    have_req = Requirement(kind="credential", schema_path="Stripe", state="have",
                           source="runtime_error", value_origin="operator",
                           bound_value_ref="operator:credential")
    objs = [
        Objective(id=1, goal="root", success_criteria="done"),
        Objective(id=3, goal="Obtain credential for Stripe",
                  success_criteria="cred", depends_on=[1], origin="backchain",
                  requirements=[have_req]),
        Objective(id=2, goal="call the API", success_criteria="data",
                  depends_on=[1, 3]),
    ]
    fold = fold_runtime_error(objectives=objs, current_obj_id=2, sub="auth",
                              tool_name="stripe_charge", service_hint="Stripe",
                              next_id=4)
    assert fold is not None
    # The reuse path re-asks precede 3 → it MUST name it as the re-ask id.
    assert getattr(fold, "reask_precede_id", None) == 3, \
        "reuse path must signal reask_precede_id so the seam un-credits it"

    # A FRESH insert (no prior precede) must NOT signal a re-ask.
    fresh = fold_runtime_error(
        objectives=[Objective(id=1, goal="root", success_criteria="done"),
                    Objective(id=2, goal="call API", success_criteria="data",
                              depends_on=[1])],
        current_obj_id=2, sub="auth", tool_name="stripe_charge",
        service_hint="Stripe", next_id=3)
    assert fresh is not None
    assert getattr(fresh, "reask_precede_id", None) is None, \
        "a fresh precede was never credited → no re-ask signal"

    # An IDEMPOTENT-PENDING (already-missing precede) must NOT signal a re-ask.
    missing_req = Requirement(kind="credential", schema_path="Stripe",
                              state="missing", source="runtime_error",
                              value_origin="operator")
    idem_objs = [
        Objective(id=1, goal="root", success_criteria="done"),
        Objective(id=3, goal="Obtain credential for Stripe",
                  success_criteria="cred", depends_on=[1], origin="backchain",
                  requirements=[missing_req]),
        Objective(id=2, goal="call the API", success_criteria="data",
                  depends_on=[1, 3]),
    ]
    idem = fold_runtime_error(objectives=idem_objs, current_obj_id=2, sub="auth",
                              tool_name="stripe_charge", service_hint="Stripe",
                              next_id=4)
    assert idem is not None and idem.already_pending is True
    assert getattr(idem, "reask_precede_id", None) is None, \
        "idempotent-pending precede is still-missing (never credited) → no re-ask signal"


def _seed_credited_precede_snapshot(tmp_path, shadow, activity, *, exec_id,
                                    precede_id=2, orig_id=1):
    """Write a resume snapshot modelling the state AFTER a credential was supplied
    and the precede was CREDITED: precede (state="have") + the precede id already in
    completed_objective_ids, the original objective depends_on=[precede]. A resume
    from this snapshot re-hydrates the stale credit — the bug's entry condition."""
    from systemu.core.models import Objective, Requirement
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot

    have_req = Requirement(kind="credential", schema_path="api_tool", state="have",
                           source="runtime_error", value_origin="operator",
                           bound_value_ref="operator:credential")
    graph = [
        Objective(id=precede_id, goal="Obtain credential for api_tool",
                  success_criteria="cred available", depends_on=[],
                  origin="backchain", requirements=[have_req]),
        Objective(id=orig_id, goal="call the api", success_criteria="got data",
                  depends_on=[precede_id]),
    ]
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b9",
        activity_id=activity.id, iteration=1,
        completed_objective_ids=[precede_id],   # the precede was CREDITED
        objective_graph=graph, next_objective_id=precede_id + 2,
    )
    write_snapshot(snap, data_dir=tmp_path)
    return snap


def test_fixA_gate_honesty_reask_uncredits_and_recloses(tmp_path, monkeypatch):
    """Fix A (seam, gate honesty): resume from a state where precede id=2 is credited
    (state="have", 2 ∈ completed_objectives). A WRONG credential re-401s the retried
    call → the reuse path re-asks precede 2 and the seam SUSPENDS. AFTER the re-ask:
      * precede 2 is NOT in completed_objectives (un-credited),
      * its requirement state == "missing",
      * the scheduling-gate expression reports the ORIGINAL objective (depends_on=[2])
        NOT ready — the gate RE-CLOSED, genuinely waiting for the re-supplied cred.
    FAILS before Fix A (precede stays stale-credited → gate open). Passes after."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    import asyncio

    vault, shadow, activity = _build_entities(tmp_path)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    exec_id = "exec_wrongcred"
    _seed_credited_precede_snapshot(data_dir, shadow, activity, exec_id=exec_id,
                                    precede_id=2, orig_id=1)

    runtime = ShadowRuntime(cfg, vault)
    # The retried call fails AGAIN with a 401 (a WRONG credential was supplied).
    fail = _mk_result(success=False, parsed={"status_code": 401},
                      stderr="HTTP 401 Unauthorized", error="401 Unauthorized")

    async def _fail_handle(decision, tools, context, current_ab, dry_run, **kw):
        tn = decision.get("tool_name", "") or "?"
        runtime._consec_tool_fails[tn] = runtime._consec_tool_fails.get(tn, 0) + 1
        return fail
    monkeypatch.setattr(runtime, "_handle_tool_call", _fail_handle)

    # Capture the live objective graph + completed set at re-suspend time by spying
    # on the snapshot capture the fold-and-suspend seam performs.
    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        return objs, sj
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    # On resume, obj id=1 retries its tool call (deps satisfied by the stale credit)
    # → it 401s again → the reuse path re-asks precede 2 and re-suspends.
    c_decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"q": "x"},
         "completes_objective": 1, "reasoning": "retry with (wrong) credential"},
        {"action": "FAIL", "reason": "should not reach"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    assert r.get("status") == "suspended_harness_escalation", r.get("status")

    # The re-ask un-credited the precede from the resume precede-credit set.
    assert 2 not in getattr(runtime, "_resume_completed_precedes", set()), \
        getattr(runtime, "_resume_completed_precedes", None)

    # ── Gate honesty: read the RE-SUSPEND snapshot (written by the seam under the
    # resumed run's FRESH execution_id — a resume mints a new id + consumes the old
    # snapshot). Its completed_objective_ids + graph reflect the post-un-credit state. ──
    from systemu.runtime.execution_snapshot import read_snapshot as _rs
    _new_eid = r.get("execution_id")
    assert _new_eid, r
    snap2 = _rs(_new_eid, data_dir=data_dir)
    assert snap2 is not None, _new_eid
    completed = set(snap2.completed_objective_ids or [])
    # (1) The precede id=2 was UN-CREDITED — no longer in completed_objectives.
    assert 2 not in completed, \
        f"Fix A: precede 2 must be un-credited on re-ask; completed={completed}"
    # (2) Its requirement was flipped back to "missing" (re-asked).
    graph = snap2.objective_graph or []
    def _as_dict(o):
        return o if isinstance(o, dict) else o.model_dump(mode="json")
    g2 = next(_as_dict(o) for o in graph if _as_dict(o).get("id") == 2)
    reqs2 = g2.get("requirements") or []
    assert reqs2 and reqs2[0].get("state") == "missing", reqs2
    # (3) THE GATE: the scheduling-gate expression must report the ORIGINAL objective
    #     (id=1, depends_on=[2]) as NOT ready — the depends_on gate is CLOSED again.
    objs_d = [_as_dict(o) for o in graph]
    ready = [o["id"] for o in objs_d
             if o["id"] not in completed
             and all(dep in completed for dep in (o.get("depends_on") or []))]
    assert 1 not in ready, \
        f"Fix A: original objective 1 must NOT be ready (gate closed); ready={ready}"


def test_fixA_no_unauthenticated_completion_via_other_action(tmp_path, monkeypatch):
    """Fix A (seam, no unauthenticated completion): during the corrupt window the LLM
    tries to advance the ORIGINAL objective via a DIFFERENT succeeding action. After
    Fix A the precede is un-credited so the objective's depends_on gate is CLOSED —
    it is NOT creditable and the run cannot finish 'successfully' UNAUTHENTICATED.

    We drive: resume → (1) retry api_tool → 401 → reuse re-ask + re-suspend. The run
    parks; the original objective 1 never lands in completed_objectives (its gate is
    shut) so no unauthenticated 'all objectives complete' path can fire."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    import asyncio

    vault, shadow, activity = _build_entities(tmp_path)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    exec_id = "exec_noauthcomplete"
    _seed_credited_precede_snapshot(data_dir, shadow, activity, exec_id=exec_id,
                                    precede_id=2, orig_id=1)

    # Neutralize the ORTHOGONAL legacy resume recredit hook (recredit_on_resume):
    # for a legacy objective with no verifier hint it trivially re-credits from
    # "durable evidence" during resume rehydration — a pre-existing mechanism
    # unrelated to the wrong-credential bug. Disable it so this test isolates the
    # ONE property Fix A controls: the credential-gate (precede) closure. Without
    # this, obj 1 would be credited by the durable-evidence shortcut, masking the
    # gate we are actually testing.
    import systemu.runtime.shadow_runtime as _srmod
    from systemu.runtime.shadow_runtime import CompletionOutcome, ObjectiveState
    monkeypatch.setattr(
        _srmod, "recredit_on_resume",
        lambda **kw: CompletionOutcome(credited=False, state=ObjectiveState()),
        raising=False)

    runtime = ShadowRuntime(cfg, vault)
    fail = _mk_result(success=False, parsed={"status_code": 401},
                      stderr="HTTP 401 Unauthorized", error="401 Unauthorized")

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        tn = decision.get("tool_name", "") or "?"
        # api_tool 401s (wrong credential); any OTHER tool "succeeds".
        if tn == "api_tool":
            runtime._consec_tool_fails[tn] = runtime._consec_tool_fails.get(tn, 0) + 1
            return fail
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    # First the api_tool retry 401s (→ reuse re-ask + re-suspend). The run parks there
    # before it can reach any other-action completion of objective 1.
    c_decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"q": "x"},
         "completes_objective": 1, "reasoning": "retry with wrong credential"},
        # If the run did NOT suspend, the LLM would try to complete obj 1 via a
        # different succeeding tool — this must never get credited.
        {"action": "TOOL_CALL", "tool_name": "other_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "sneak completion unauthenticated"},
        {"action": "FINISH", "reason": "done"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    # The run parked on the re-ask (did not finish unauthenticated).
    assert r.get("status") == "suspended_harness_escalation", r.get("status")
    assert r.get("status") != "success"
    # The re-suspend snapshot (under the resumed run's FRESH execution_id) shows
    # objective 1 NOT credited (gate shut).
    from systemu.runtime.execution_snapshot import read_snapshot as _rs
    _new_eid = r.get("execution_id")
    assert _new_eid, r
    snap2 = _rs(_new_eid, data_dir=data_dir)
    assert snap2 is not None, _new_eid
    completed = set(snap2.completed_objective_ids or [])
    assert 1 not in completed, \
        f"objective 1 must NOT be creditable during the corrupt window; completed={completed}"
    assert 2 not in completed, "precede 2 must be un-credited"


# ═════════════════════════════════════════════════════════════════════════════
# Part 8 — Fix B (MEDIUM, classifier precision): explicit fold / don't-fold matrix.
#          Every MUST-FOLD shape → a CONFIDENT http_error with the right auth/
#          semantic sub-class. Every MUST-NOT-FOLD shape → NOT a confident http
#          status (category != http_error, sub == "other").
# ═════════════════════════════════════════════════════════════════════════════

# (shape, expected_sub) — real library/tool 401/403/422/404 + structured shapes.
_MUST_FOLD = [
    # requests raise_for_status()
    ("401 Client Error: Unauthorized for url: https://api.stripe.com/v1/charges", "auth"),
    ("403 Client Error: Forbidden", "auth"),
    ("422 Client Error: Unprocessable Entity", "semantic"),
    ("404 Client Error: Not Found", "semantic"),
    # aiohttp
    ("ClientResponseError: 401, message='Unauthorized'", "auth"),
    ("ClientResponseError: 403, message='Forbidden'", "auth"),
    # HTTP / structured-parsed
    ("HTTP 401", "auth"),
    ("HTTP/1.1 403 Forbidden", "auth"),
    ("status_code=404", "semantic"),
    ("status: 403", "auth"),
    ('{"status_code":401}', "auth"),
    # code + reason at start / colon form
    ("401 Unauthorized", "auth"),
    ("401: Unauthorized", "auth"),
    # OAuth (best-effort — a (401)/401 adjacent to an auth-ish token)
    ("invalid_token: the access token expired (401)", "auth"),
    ("OAuthError: 401 invalid_grant", "auth"),
]

# benign counts / ids / frames in FAILED output — must NOT fold.
_MUST_NOT_FOLD = [
    "GET /v1/items/401 request failed",
    'File "httpclient.py", line 401',
    "app.py:404",
    "error 40100",
    "query failed, returned 404 rows",
    "job failed, status 404 tasks remaining",
    "operation failed; response had 404 duplicate keys",
    "completed with 404 items then errored",
    "the request returned 404 matching users",
    "row status for id 403 not found",
]


# ═════════════════════════════════════════════════════════════════════════════
# Part 7b — Fix C (ROOT-CAUSE completion): the credential-gate invariant must hold
#           at ALL objective-credit sites, not just the resume-recredit hook.
#
#   `_recredit_blocked_ids` guarded ONLY the durable-evidence recredit-on-resume
#   loop. Three other credit/finalize sites were UNGUARDED:
#     1. the LIVE per-iteration `completes_objective` credit — objective_verifier
#        soft-passes verifier=None objectives, so a resume WITHOUT a credential
#        could credit the precede + the original and reach status="success" with
#        the runtime_error requirement STILL missing (the HIGH repro).
#     2. the COMPLETE goal-level accept — a goal-verifier PASS knows nothing about
#        the backchain precede, so it finalizes success while the credential is
#        missing.
#     3. the stuck-park goal-level accept — same, at the stuck seam.
#
#   The fix computes `blocked = _recredit_blocked_ids(objectives)` over the LIVE
#   objective list (which after a fold carries the precede + the original's
#   updated depends_on) and (1) SKIPS the live credit for a blocked id, (2/3) does
#   NOT finalize success while `blocked` is non-empty.
# ═════════════════════════════════════════════════════════════════════════════

def _seed_gated_run_snapshot(data_dir, shadow, activity, *, exec_id,
                             precede_id, orig_id):
    """A resume snapshot for the LIVE-credit repro. The persisted objective_graph
    carries the B9 fold mutation: a backchain precede (state="missing" runtime_error
    credential requirement) gating ``orig_id`` via its ``depends_on``. NOTHING is
    pre-credited (completed_objective_ids empty), so the run resumes with both the
    precede and the original still pending — the exact state after a 401 fold+suspend
    when the operator resumes WITHOUT supplying a credential (no runtime_fold grant).
    The static scroll (built separately) carries orig_id (verifier=None, no
    depends_on) so objective_verifier soft-passes it."""
    from systemu.core.models import Objective, Requirement
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot

    missing_req = Requirement(kind="credential", schema_path="api_tool",
                              state="missing", source="runtime_error",
                              value_origin="operator",
                              rationale="need a credential for api_tool")
    graph = [
        Objective(id=precede_id, goal="Obtain credential for api_tool",
                  success_criteria="cred available", depends_on=[],
                  origin="backchain", requirements=[missing_req]),
        Objective(id=orig_id, goal="call the api", success_criteria="got data",
                  depends_on=[precede_id]),
    ]
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b9",
        activity_id=activity.id, iteration=1,
        completed_objective_ids=[], objective_graph=graph,
        next_objective_id=precede_id + 5,
    )
    write_snapshot(snap, data_dir=data_dir)
    return snap


def _neutralize_recredit_hook(monkeypatch):
    """Disable the ORTHOGONAL resume durable-evidence recredit hook so these tests
    isolate the credit site under test (the LIVE loop / the goal-accepts), not the
    already-guarded resume-recredit loop. Mirrors the fixA test's isolation."""
    import systemu.runtime.shadow_runtime as _srmod
    from systemu.runtime.shadow_runtime import CompletionOutcome, ObjectiveState
    monkeypatch.setattr(
        _srmod, "recredit_on_resume",
        lambda **kw: CompletionOutcome(credited=False, state=ObjectiveState()),
        raising=False)


def test_fixC_live_credit_blocked_objective_never_reaches_success(tmp_path, monkeypatch):
    """Fix C (HIGH repro — the LIVE per-iteration credit hole): resume a 401-folded run
    WITHOUT an operator credential (no runtime_fold grant), then the LLM issues
    succeeding TOOL_CALLs claiming completes_objective on the precede (id=2) then the
    original (id=1). objective_verifier soft-passes both (verifier=None), so BEFORE the
    fix both credit → len(completed) >= total → status="success" with the credential
    STILL missing. AFTER the fix `blocked = _recredit_blocked_ids(objectives)` == {2, 1}
    (2 carries the missing runtime_error req; 1 depends_on 2) → BOTH live credits are
    SKIPPED → the run never reaches success (it drives on to the terminal FAIL)."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.core.models import Objective
    import asyncio

    # STATIC scroll tree: obj 1 credential-gated (verifier=None, no depends_on here —
    # the depends_on=[2] mutation lives only in the persisted graph).
    scroll_objs = [
        Objective(id=1, goal="call the api", success_criteria="got data"),
    ]
    vault, shadow, activity = _build_entities_objs(tmp_path, scroll_objs)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)
    _neutralize_recredit_hook(monkeypatch)

    exec_id = "exec_live_credit_gated"
    _seed_gated_run_snapshot(data_dir, shadow, activity, exec_id=exec_id,
                             precede_id=2, orig_id=1)

    runtime = ShadowRuntime(cfg, vault)

    # Every tool "succeeds" (result.success=True) — the point is the LLM CLAIMS the
    # objective on a succeeding tool with the credential still absent.
    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    # Capture the steering observations the skip path emits (proves the guard fired).
    seen_obs = []
    import systemu.runtime.context_builder as _cb
    _orig_add_obs = _cb.ExecutionContext.add_observation

    def _spy_add_obs(self, obs, ab):
        try:
            seen_obs.append(obs)
        except Exception:
            pass
        return _orig_add_obs(self, obs, ab)
    monkeypatch.setattr(_cb.ExecutionContext, "add_observation", _spy_add_obs)

    c2_decisions = [
        # Claim the precede (id=2) on a succeeding tool — soft-passes verifier=None.
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 2, "reasoning": "claim the precede unauthenticated"},
        # Then claim the original (id=1) — soft-passes verifier=None.
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {},
         "completes_objective": 1, "reasoning": "claim the original unauthenticated"},
        # Deterministic terminal so the loop cannot spin: if the fix skipped both
        # credits (never hit the >= total success path), the run FAILs here.
        {"action": "FAIL", "reason": "should reach here only if credits were skipped"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c2_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    # THE INVARIANT: the run must NOT finalize success with the credential missing.
    assert r.get("status") != "success", (
        "Fix C: a run gated on a missing runtime_error requirement must NOT reach "
        f"status='success' via the live completes_objective credit; got {r.get('status')}")
    # The guard fired: a steering observation for the blocked objective was emitted.
    _blocked_msgs = [o for o in seen_obs
                     if isinstance(o, dict)
                     and o.get("type") == "objective_blocked_credential_gate"]
    assert _blocked_msgs, (
        "Fix C: the live-credit skip must emit a steering observation for the "
        f"blocked objective; observations seen: {seen_obs}")


def test_fixC_complete_goal_accept_blocked_by_missing_credential(tmp_path, monkeypatch):
    """Fix C (COMPLETE goal-accept variant): the LLM issues COMPLETE while a
    runtime_error requirement is still missing, and the goal-verifier PASSES (it knows
    nothing about the backchain precede). BEFORE the fix the COMPLETE goal-level accept
    finalizes status="success"; AFTER the fix `blocked` is non-empty → the COMPLETE is
    NOT accepted (the run does not finalize success)."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    from systemu.core.models import Objective
    import asyncio

    scroll_objs = [
        Objective(id=1, goal="call the api", success_criteria="got data"),
    ]
    vault, shadow, activity = _build_entities_objs(tmp_path, scroll_objs)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)
    _neutralize_recredit_hook(monkeypatch)

    exec_id = "exec_complete_gated"
    _seed_gated_run_snapshot(data_dir, shadow, activity, exec_id=exec_id,
                             precede_id=2, orig_id=1)

    runtime = ShadowRuntime(cfg, vault)

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    # Force the GOAL verifier to PASS (it knows nothing about the precede). This is the
    # exact hazard: a goal-level pass would otherwise finalize the run unauthenticated.
    import systemu.runtime.goal_verifier as _gv
    monkeypatch.setattr(_gv, "verify_goal",
                        lambda **kw: {"verified": True, "reason": "artifact present"})

    c2_decisions = [
        {"action": "COMPLETE", "summary": "done (unauthenticated)"},
        {"action": "FAIL", "reason": "reached only if COMPLETE was rejected"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c2_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    assert r.get("status") != "success", (
        "Fix C: a COMPLETE goal-level accept must NOT finalize success while a "
        f"runtime_error requirement is missing; got {r.get('status')}")


def test_fixC_stuck_park_goal_accept_blocked_by_missing_credential(tmp_path, monkeypatch):
    """Fix C (stuck-park goal-accept variant): the run reaches the stuck seam and
    `_intent_goal_success` returns True (goal met from durable evidence) while a
    runtime_error requirement is still missing. BEFORE the fix the stuck-park goal
    accept finalizes status="success"; AFTER the fix `blocked` is non-empty → it does
    NOT finalize success (it falls through to the honest park path)."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    from systemu.core.models import Objective
    import asyncio

    scroll_objs = [
        Objective(id=1, goal="call the api", success_criteria="got data"),
    ]
    vault, shadow, activity = _build_entities_objs(tmp_path, scroll_objs)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)
    _neutralize_recredit_hook(monkeypatch)

    exec_id = "exec_stuck_gated"
    _seed_gated_run_snapshot(data_dir, shadow, activity, exec_id=exec_id,
                             precede_id=2, orig_id=1)

    runtime = ShadowRuntime(cfg, vault)

    # Every tool "succeeds" but never credits (the LLM makes no valid claim), so the
    # no-progress counter climbs to the stuck bound.
    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    # Force the stuck-park goal-level check to PASS — the exact hazard.
    monkeypatch.setattr(_sr, "_intent_goal_success", lambda **kw: True)
    # Trip the stuck trigger deterministically on the first check.
    monkeypatch.setattr(runtime, "_stuck_trigger",
                        lambda: (True, "no-progress (forced for test)"))
    # Disable the auto-coach self-steer so the stuck seam reaches the goal-accept.
    cfg.auto_coach_enabled = False

    c2_decisions = [
        # A THINK burns an iteration without crediting; the forced _stuck_trigger fires
        # at the post-iteration stuck check → the stuck-park goal-accept is evaluated.
        {"action": "THINK", "thought": "considering the situation"},
        {"action": "FAIL", "reason": "reached only if the stuck-park accept was blocked"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c2_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    assert r.get("status") != "success", (
        "Fix C: a stuck-park goal-level accept must NOT finalize success while a "
        f"runtime_error requirement is missing; got {r.get('status')}")


def test_fixC_happy_path_credential_supplied_still_completes(tmp_path, monkeypatch):
    """Fix C (happy path unaffected): when the operator supplies the credential, the
    B9 _apply_resume_fold_credit path flips the precede's requirement "missing"→"have"
    and credits the precede → `blocked` becomes empty → the original objective credits
    at the LIVE site and the run reaches status="success" normally. Proves the gate is
    lifted only via the sanctioned path, and the fix does not block a legitimate run."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    import asyncio
    import json as _json

    vault, shadow, activity = _build_entities(tmp_path)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # ── CYCLE 1: 401 fold + suspend ─────────────────────────────────────────
    fail = _mk_result(success=False, parsed={"status_code": 401},
                      stderr="HTTP 401 Unauthorized", error="401 Unauthorized")
    runtime = ShadowRuntime(cfg, vault)

    async def _fail_handle(decision, tools, context, current_ab, dry_run, **kw):
        tn = decision.get("tool_name", "") or "?"
        runtime._consec_tool_fails[tn] = runtime._consec_tool_fails.get(tn, 0) + 1
        return fail
    monkeypatch.setattr(runtime, "_handle_tool_call", _fail_handle)

    c1_decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"q": "x"},
         "completes_objective": 1, "reasoning": "hit the api"},
        {"action": "FAIL", "reason": "should not reach"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c1_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r1 = asyncio.run(runtime.execute(shadow, activity))
    assert r1.get("status") == "suspended_harness_escalation", r1.get("status")
    exec_id = r1.get("execution_id")

    from systemu.runtime.execution_snapshot import read_snapshot as _rs
    snap1 = _rs(exec_id, data_dir=data_dir)
    _pend = next((n for n in snap1.sticky_notes if n.startswith("__HARNESS_PENDING__::")), None)
    _pend_spec = _json.loads(_pend.split("::", 2)[2])["spec"]
    _precede_id = _pend_spec.get("precede_id")

    grant_payload = {
        "kind": "input", "param_answers": {},
        "requested_schema": _pend_spec.get("requested_schema") or {},
        "pending_tool": _pend_spec.get("pending_tool"),
        "runtime_fold": True, "requirement_kind": "credential",
        "requirement_schema_path": "api_tool", "precede_id": _precede_id,
    }
    _stamp_grant_note(data_dir, exec_id, grant_payload)

    # ── CYCLE 2: resume with the credential. The precede is credited via
    #    _apply_resume_fold_credit → blocked empties → the original credits + success. ──
    async def _ok_handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _ok_handle)

    c2_decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"q": "x"},
         "completes_objective": 1, "reasoning": "retry with credential"},
        {"action": "FAIL", "reason": "should not reach — the run should succeed"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c2_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r2 = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    # The precede was credited via the sanctioned path and the original objective then
    # credited at the LIVE site → the run reached success.
    assert _precede_id in getattr(runtime, "_resume_completed_precedes", set()), \
        getattr(runtime, "_resume_completed_precedes", None)
    assert r2.get("status") == "success", (
        "Fix C: with the credential supplied (precede credited via the sanctioned "
        f"path), the original objective must credit and the run succeed; got {r2.get('status')}")


@pytest.mark.parametrize("shape,expected_sub", _MUST_FOLD)
def test_fixB_must_fold(shape, expected_sub):
    """Fix B: every real library/tool/structured 4xx shape → a CONFIDENT http_error
    with the correct auth/semantic sub-class (so the B9 seam folds a precede)."""
    from systemu.runtime.failure_classifier import (
        classify_tool_result, http_error_subclass,
    )
    # structured JSON body → give it as a parsed status_code too, matching how the
    # runtime surfaces a parsed payload; free-text goes through .error.
    if shape == '{"status_code":401}':
        r = _mk_result(parsed={"status_code": 401})
    else:
        r = _mk_result(error=shape)
    assert classify_tool_result(r).category == "http_error", (shape,)
    assert http_error_subclass(r) == expected_sub, (shape, http_error_subclass(r))


@pytest.mark.parametrize("shape", _MUST_NOT_FOLD)
def test_fixB_must_not_fold(shape):
    """Fix B: every benign count/id/frame in FAILED output must NOT be a confident
    http status → classify_tool_result.category != http_error AND
    http_error_subclass == "other" (never auth/semantic, never a spurious fold)."""
    from systemu.runtime.failure_classifier import (
        classify_tool_result, http_error_subclass, _anchored_http_status,
    )
    assert _anchored_http_status(shape) is None, (shape,)
    assert http_error_subclass(_mk_result(error=shape)) == "other", (shape,)
    c = classify_tool_result(_mk_result(error=shape))
    assert c.category != "http_error", (shape, c.category)


# ═════════════════════════════════════════════════════════════════════════════
# Part 9 — Fix C (HIGH, safety): the durable-evidence recredit-on-resume hook must
#          NOT re-credit a CREDENTIAL/DECISION-gated objective. The pre-existing
#          recredit_on_resume hook trivially passes a verifier=None objective
#          (objective_verifier short-circuits BEFORE any durable check), which would
#          UNDO B9's Fix A: an objective gated on a still-missing runtime_error
#          requirement (a backchain precede) gets marked complete → an
#          unauthenticated "success". The guard derives a blocked_ids set from the
#          PERSISTED graph (snap.objective_graph — which carries the mutation) and
#          SKIPS those objectives in the recredit loop. Legacy resumes (no
#          runtime_error requirements / empty graph) are byte-unchanged.
# ═════════════════════════════════════════════════════════════════════════════

def _build_entities_objs(tmp_path, objectives):
    """Like _build_entities but the scroll carries an arbitrary objective list (so
    the recredit loop, which iterates the STATIC scroll tree, sees >1 objective)."""
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

    shadow = Shadow(id="shadow_b9", name="B9 Shadow", description="t",
                    system_prompt="t", status=ShadowStatus.AWAKENED)
    vault.save_shadow(shadow)
    tool = Tool(id="tool_b9", name="api_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/api_tool.py")
    vault.save_tool(tool)
    scroll = Scroll(id="scroll_b9", name="B9 Scroll", source_session_id="s",
                    raw_instructions_path="", narrative_md="",
                    objectives=objectives)
    vault.save_scroll(scroll)
    activity = Activity(id="act_b9", name="B9 Activity", scroll_id=scroll.id,
                        required_tool_ids=["tool_b9"], required_skill_ids=[],
                        assigned_shadow_id=shadow.id)
    vault.save_activity(activity)
    return vault, shadow, activity


class _CaptureAbort(Exception):
    """Sentinel raised by the resolve-spy once it has captured everything the Fix-C
    recredit-guard tests assert on, to unwind execute() immediately. The recredit
    hook + graph rehydration both run BEFORE _resolve_objectives_for_run, so by the
    capture point the guarded ``completed_objectives`` set and the rehydrated
    ``objectives`` list are final — there is nothing left to observe by letting the
    main loop run on (and letting it run on can block on a re-fold/re-suspend of the
    still-gated objective, which is not what these tests exercise)."""


def _capture_completed_at_resolve(monkeypatch):
    """Spy on _resolve_objectives_for_run to snapshot the LIVE ``completed_objectives``
    local at the exact point AFTER the resume recredit loop has run (the loop is the
    last thing to mutate that set before this call). Drives the REAL recredit hook —
    no monkeypatch of recredit_on_resume. Returns a dict that will hold
    ``{"completed": set(...), "objectives": [...], "context": <ctx>}``; the spy then
    raises ``_CaptureAbort`` to unwind execute() (the tests catch it)."""
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
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        captured["objectives"] = objs
        # Everything the Fix-C guard tests assert on is now final — unwind execute()
        # rather than letting the main loop drive on (which could re-fold the still-
        # gated objective and block on a harness re-suspend).
        raise _CaptureAbort()

    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)
    return captured


def _seed_multiobj_fold_snapshot(data_dir, shadow, activity, *, exec_id,
                                 precede_id, orig_id, other_id):
    """A resume snapshot for the HIGH repro. ``other_id`` was already credited (so
    ``completed_objective_ids`` is non-empty and the recredit loop RUNS), and the
    persisted ``objective_graph`` carries the B9 mutation: a backchain precede
    (state="missing" runtime_error credential requirement) gating ``orig_id`` via its
    ``depends_on``. The STATIC scroll (built separately) still has ``orig_id`` with
    verifier=None and no depends_on — so recredit_on_resume trivially passes it
    UNLESS the guard blocks it."""
    from systemu.core.models import Objective, Requirement
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot

    missing_req = Requirement(kind="credential", schema_path="api_tool",
                              state="missing", source="runtime_error",
                              value_origin="operator",
                              rationale="need a credential for api_tool")
    graph = [
        Objective(id=precede_id, goal="Obtain credential for api_tool",
                  success_criteria="cred available", depends_on=[],
                  origin="backchain", requirements=[missing_req]),
        Objective(id=orig_id, goal="call the api", success_criteria="got data",
                  depends_on=[precede_id]),
        Objective(id=other_id, goal="prepare the request",
                  success_criteria="request prepared", depends_on=[]),
    ]
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b9",
        activity_id=activity.id, iteration=1,
        completed_objective_ids=[other_id],       # a DIFFERENT objective was credited
        objective_graph=graph, next_objective_id=precede_id + 5,
    )
    write_snapshot(snap, data_dir=data_dir)
    return snap


def test_fixC_recredit_guard_skips_credential_gated_objective(tmp_path, monkeypatch):
    """Fix C (HIGH repro): a multi-objective resume where a DIFFERENT objective (id=3)
    was already credited (so the recredit loop RUNS) and the ORIGINAL objective (id=1,
    verifier=None) is gated on a still-MISSING backchain credential precede (id=2).

    The pre-existing recredit_on_resume hook trivially passes verifier=None objectives
    (the verifier short-circuits BEFORE any durable check), which — WITHOUT the guard —
    re-credits obj 1 with the credential STILL missing → an unauthenticated completion
    that UNDOES B9's Fix A gate.

    Drive the REAL resume with the recredit hook LIVE (no monkeypatch). Assert: after
    rehydration obj 1 is NOT in completed_objectives (the guard skipped it), and the
    scheduling gate reports obj 1 NOT ready (its depends_on=[2] gate is closed —
    precede 2 is missing + uncredited). FAILS before the fix; passes after."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.core.models import Objective
    import asyncio

    # STATIC scroll tree: obj 1 (credential-gated, verifier=None, NO depends_on here —
    # the depends_on=[2] mutation lives only in the persisted graph) + obj 3 (the OTHER
    # objective). The recredit loop iterates THIS list.
    scroll_objs = [
        Objective(id=1, goal="call the api", success_criteria="got data"),
        Objective(id=3, goal="prepare the request",
                  success_criteria="request prepared"),
    ]
    vault, shadow, activity = _build_entities_objs(tmp_path, scroll_objs)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    exec_id = "exec_multiobj_fold"
    _seed_multiobj_fold_snapshot(data_dir, shadow, activity, exec_id=exec_id,
                                 precede_id=2, orig_id=1, other_id=3)

    runtime = ShadowRuntime(cfg, vault)

    # No tool call needs to succeed — the recredit hook fires during rehydration,
    # BEFORE the main loop. A benign FINISH lets execute() return promptly after the
    # capture point (_resolve_objectives_for_run) is reached.
    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    captured = _capture_completed_at_resolve(monkeypatch)

    # The recredit hook + graph rehydration run BEFORE _resolve_objectives_for_run;
    # the spy captures there and raises _CaptureAbort to unwind execute() (letting the
    # main loop drive on would re-fold the still-gated obj 1 and block on a re-suspend).
    with patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        try:
            asyncio.run(runtime.execute(
                shadow, activity, resume_from_execution_id=exec_id))
        except _CaptureAbort:
            pass

    completed = captured.get("completed")
    assert completed is not None, "spy never captured completed_objectives"
    # The OTHER objective is legitimately credited (from the snapshot).
    assert 3 in completed, f"the pre-credited other objective must survive; {completed}"
    # THE FIX: obj 1 (credential-gated) must NOT be recredited by the durable shortcut.
    assert 1 not in completed, (
        "Fix C: the credential-gated objective 1 must NOT be re-credited via the "
        f"durable-evidence recredit-on-resume hook (credential still missing); "
        f"completed={completed}")
    # The precede itself is never touched by the recredit loop (not in the static scroll).
    assert 2 not in completed, f"precede 2 must stay uncredited; {completed}"

    # Gate honesty: on the rehydrated (mutated) graph, obj 1 (depends_on=[2]) must be
    # NOT ready — precede 2 is missing + uncredited, so the gate is CLOSED.
    objs = captured.get("objectives")
    assert objs is not None
    graph_ids = {o.id: o for o in objs}
    assert 2 in graph_ids, "resume must rehydrate the persisted graph with the precede"
    ready = [o.id for o in objs
             if o.id not in completed
             and all(dep in completed for dep in (o.depends_on or []))]
    assert 1 not in ready, \
        f"Fix C: obj 1 must NOT be ready (credential gate closed); ready={ready}"


def test_fixC_legacy_recredit_unaffected(tmp_path, monkeypatch):
    """Fix C (no regression): a resume with a LEGACY verifier=None objective, an EMPTY
    persisted objective_graph, and NO runtime_error requirements → blocked_ids is
    empty → the recredit hook STILL credits the legacy objective from durable evidence.
    Proves the guard did not break legacy durable-evidence recredit."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.core.models import Objective
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    import asyncio

    # Two legacy objectives; obj 3 already credited (loop runs), obj 1 legacy vf=None.
    scroll_objs = [
        Objective(id=1, goal="write the report", success_criteria="report exists"),
        Objective(id=3, goal="gather inputs", success_criteria="inputs gathered"),
    ]
    vault, shadow, activity = _build_entities_objs(tmp_path, scroll_objs)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    exec_id = "exec_legacy_recredit"
    # LEGACY snapshot: empty objective_graph (pre-G1), obj 3 credited.
    snap = ExecutionSnapshot(
        execution_id=exec_id, shadow_id=shadow.id, scroll_id="scroll_b9",
        activity_id=activity.id, iteration=1,
        completed_objective_ids=[3], objective_graph=[], next_objective_id=1,
    )
    write_snapshot(snap, data_dir=data_dir)

    runtime = ShadowRuntime(cfg, vault)

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    captured = _capture_completed_at_resolve(monkeypatch)

    # The recredit hook runs BEFORE _resolve_objectives_for_run (where the spy captures
    # + raises _CaptureAbort to unwind execute()); the legacy obj 1 is credited by the
    # hook before that point, so the capture reflects the full recredit outcome.
    with patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        try:
            asyncio.run(runtime.execute(
                shadow, activity, resume_from_execution_id=exec_id))
        except _CaptureAbort:
            pass

    completed = captured.get("completed")
    assert completed is not None, "spy never captured completed_objectives"
    assert 3 in completed, f"pre-credited obj 3 must survive; {completed}"
    # The legacy verifier=None objective 1 IS still recredited (blocked_ids empty).
    assert 1 in completed, (
        "Fix C must NOT block legacy durable-evidence recredit: a verifier=None "
        f"objective with no runtime_error requirement / empty graph still credits; "
        f"completed={completed}")


def test_fixC_correct_credential_path_still_credits(tmp_path, monkeypatch):
    """Fix C (no regression on the legitimate credit path): when the operator supplies
    a CORRECT credential, the B9 _apply_resume_fold_credit path flips the precede's
    requirement "missing"→"have", credits the precede, and the original objective's
    gate OPENS + it retries + completes. The recredit guard (which only skips the
    DURABLE shortcut) must NOT block this explicit credit path.

    This mirrors the shipped round-trip test but re-asserts it AFTER the guard lands:
    CYCLE1 401 fold+suspend → stamp the operator credential → CYCLE2 resume credits
    the precede + the original objective retries and finishes (no re-suspend)."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    import asyncio
    import json as _json

    vault, shadow, activity = _build_entities(tmp_path)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_snapshot_io(monkeypatch, data_dir)
    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card_fake", raising=False)

    # ── CYCLE 1: 401 fold + suspend ─────────────────────────────────────────
    fail = _mk_result(success=False, parsed={"status_code": 401},
                      stderr="HTTP 401 Unauthorized", error="401 Unauthorized")
    runtime = ShadowRuntime(cfg, vault)

    async def _fail_handle(decision, tools, context, current_ab, dry_run, **kw):
        tn = decision.get("tool_name", "") or "?"
        runtime._consec_tool_fails[tn] = runtime._consec_tool_fails.get(tn, 0) + 1
        return fail
    monkeypatch.setattr(runtime, "_handle_tool_call", _fail_handle)

    c1_decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"q": "x"},
         "completes_objective": 1, "reasoning": "hit the api"},
        {"action": "FAIL", "reason": "should not reach"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c1_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r1 = asyncio.run(runtime.execute(shadow, activity))
    assert r1.get("status") == "suspended_harness_escalation", r1.get("status")
    exec_id = r1.get("execution_id")

    from systemu.runtime.execution_snapshot import read_snapshot as _rs
    snap1 = _rs(exec_id, data_dir=data_dir)
    _pend = next((n for n in snap1.sticky_notes if n.startswith("__HARNESS_PENDING__::")), None)
    _pend_spec = _json.loads(_pend.split("::", 2)[2])["spec"]
    _precede_id = _pend_spec.get("precede_id")

    # Operator supplies the CORRECT credential.
    grant_payload = {
        "kind": "input", "param_answers": {},
        "requested_schema": _pend_spec.get("requested_schema") or {},
        "pending_tool": _pend_spec.get("pending_tool"),
        "runtime_fold": True, "requirement_kind": "credential",
        "requirement_schema_path": "api_tool", "precede_id": _precede_id,
    }
    _stamp_grant_note(data_dir, exec_id, grant_payload)

    # ── CYCLE 2: resume. Now the tool SUCCEEDS (correct credential present). ──
    async def _ok_handle(decision, tools, context, current_ab, dry_run, **kw):
        return _mk_result(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _ok_handle)

    captured = {}
    orig_resolve = _sr._resolve_objectives_for_run

    def _spy(**kw):
        objs, sj = orig_resolve(**kw)
        captured["context"] = kw.get("context")
        return objs, sj
    monkeypatch.setattr(_sr, "_resolve_objectives_for_run", _spy)

    c2_decisions = [
        {"action": "TOOL_CALL", "tool_name": "api_tool", "parameters": {"q": "x"},
         "completes_objective": 1, "reasoning": "retry with credential"},
        {"action": "FINISH", "reason": "done"},
    ]
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=c2_decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        r2 = asyncio.run(runtime.execute(
            shadow, activity, resume_from_execution_id=exec_id))

    # The explicit credit path still fires: the precede was credited (guard did not
    # block the legitimate _apply_resume_fold_credit path) and the run did not re-suspend.
    assert _precede_id in getattr(runtime, "_resume_completed_precedes", set()), \
        getattr(runtime, "_resume_completed_precedes", None)
    assert r2.get("status") != "suspended_harness_escalation", r2.get("status")
    # The precede's requirement was flipped "missing"→"have" on the re-persisted graph.
    ctx = captured.get("context")
    graph = getattr(ctx, "_objective_graph", None)
    assert graph, "resume must re-persist the satisfied graph"
    g_precede = next((o for o in graph if o.get("id") == _precede_id), None)
    _greqs = (g_precede or {}).get("requirements") or []
    assert _greqs and _greqs[0].get("state") == "have", _greqs
