"""R-A13 Stage-3a — the operator_attest ENFORCE fallback (behind the flag).

PART A (enqueue + suspend): when, under ``SYSTEMU_S4_STAMP=enforce``, a NON-money
external effect cannot be independently confirmed AND no independent readback channel
was even available (no ``readback_url`` in the effect envelope), the runtime surfaces
an ``operator_attest:<oid>`` operator card and PARKS (``suspended_operator_attest``)
instead of only a silent not-credit. Money-move is HARD-excluded; an effect that HAD a
channel is not short-cut to attest; OFF/SHADOW are byte-identical (no card).

PART B (resume → credit): the resolve → sticky → resume round-trip. The resume TRIGGER
(``_dispatch_resume`` / the reconciler) recognizes the attest card and stashes an
``__OPERATOR_ATTEST__`` sticky; the resume-start APPLIER peels it and, for "Attest
occurred" on a NON-money objective, runs ``verify(strategy="operator_attest")`` +
persists the confirmed evidence so the S4 resume short-circuit credits the objective
with the effectful tool NEVER re-invoked. Money-move can never credit via attest.

All Part-A / AC-2 tests drive the REAL execute()/resume path (never a synthetic
evidence dict — the credit comes from a real verify() call in the applier).
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import patch

from systemu.runtime.effect_tags import EffectTag

# Reuse the shipped live-credit + resume harnesses verbatim.
from test_s3_credit_wiring import (
    _build_entities_objs, _redirect_snapshot_io, _external_obj, _softpass_outcome,
    _drive_live_credit,
)


# ─────────────────────────────────────────────────────────────────────────────
#  Harness helpers
# ─────────────────────────────────────────────────────────────────────────────

class _NoopReadbackClient:
    """Neutralises the ENFORCE-mode ProdReadbackClient injection so no test does
    real network I/O. Returns no observed tokens ⇒ the hardened readback never
    confirms. Passing it to ``_drive_live_credit`` overrides the prod client."""

    def __init__(self):
        self.urls = []

    def readback(self, url):
        self.urls.append(url)
        return {"observed_tokens": [], "response_body": "not found"}


def _nonmoney_tool():
    """A tool with a KNOWN non-money effect (net_mutate) so the objective classifies
    NON-money — required for the attest fallback to apply (a requires_external obj with
    NO known effect tag is money-move via the fail-closed fallback)."""
    from systemu.core.models import Tool, ToolStatus, ToolType
    return Tool(id="tool_s3w", name="api_tool", description="t",
                tool_type=ToolType.PYTHON_FUNCTION, status=ToolStatus.DEPLOYED,
                enabled=True,
                implementation_path="vault/tools/implementations/api_tool.py",
                effect_tags=[EffectTag.NET_MUTATE.value])


def _spy_attest_cards(monkeypatch):
    """Spy InboxQueue.enqueue; return a list that collects (dedup, gate_type,
    kind_marker) for every enqueued card so a test can assert an attest card was /
    was not raised."""
    cards = []
    import systemu.interface.command.inbox as _inbox
    _orig = _inbox.InboxQueue.enqueue

    def _spy(self, descriptor, *a, **k):
        try:
            cards.append({
                "dedup": getattr(descriptor, "dedup", ""),
                "gate_type": k.get("gate_type", ""),
                "kind_marker": (k.get("context_extras") or {}).get("kind_marker", ""),
            })
        except Exception:
            cards.append({"dedup": "?", "gate_type": "?", "kind_marker": "?"})
        return _orig(self, descriptor, *a, **k)
    monkeypatch.setattr(_inbox.InboxQueue, "enqueue", _spy)
    return cards


def _attest_cards(cards):
    return [c for c in cards
            if c.get("kind_marker") == "operator_attest"
            or str(c.get("dedup", "")).startswith("operator_attest:")]


# ─────────────────────────────────────────────────────────────────────────────
#  PART A — enqueue + suspend
# ─────────────────────────────────────────────────────────────────────────────

def test_ac1_nonmoney_no_channel_enforce_enqueues_and_parks(tmp_path, monkeypatch):
    """AC-1: a NON-money external effect in ENFORCE that could not be confirmed and
    had NO independent readback channel (no readback_url) → an operator_attest card
    is enqueued, the run parks (suspended_operator_attest), it is NOT credited, and
    the UNVERIFIED_EXTERNAL observation is still present."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    obs = []
    cards = _spy_attest_cards(monkeypatch)
    # NON-money envelope with NO readback_url ⇒ no independent channel.
    tool_parsed = {"ok": True, "external": {"strategy": "web_assertion",
                                            "observed_text": "done"}}
    _, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_external_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=_NoopReadbackClient(), spy_obs=obs,
        tool=_nonmoney_tool())

    assert result.get("status") == "suspended_operator_attest", (
        "a non-money unconfirmed external effect with no independent channel must "
        f"PARK for operator attestation in ENFORCE; got {result.get('status')}")
    assert _attest_cards(cards), (
        f"an operator_attest card must be enqueued; cards={cards}")
    assert result.get("operator_card_id"), "the suspend must carry the card id"
    # not credited + the existing UNVERIFIED_EXTERNAL observation is UNCHANGED.
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert not (ev and ev.get("confirmed") is True), f"must not be credited; store={store}"
    unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
    assert unv, f"UNVERIFIED_EXTERNAL must still be emitted; saw {obs}"


def test_ac1b_money_move_no_attest_card(tmp_path, monkeypatch):
    """AC-1b: an unconfirmed MONEY-MOVE external effect → NO operator_attest card
    (attestation can never credit a money-move); UNVERIFIED_EXTERNAL emitted; not
    credited. The default tool (no effect tag) + requires_external ⇒ money-move."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    obs = []
    cards = _spy_attest_cards(monkeypatch)
    tool_parsed = {"ok": True, "external": {"strategy": "web_assertion",
                                            "observed_text": "paid"}}
    obj = _external_obj(goal="pay the $500 invoice via the checkout API",
                        success_criteria="payment confirmed")
    _, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[obj], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=_NoopReadbackClient(), spy_obs=obs)

    assert not _attest_cards(cards), (
        f"a money-move must NEVER get an operator_attest card; cards={cards}")
    assert result.get("status") != "suspended_operator_attest"
    unv = [o for o in obs if isinstance(o, dict) and o.get("type") == "UNVERIFIED_EXTERNAL"]
    assert unv, f"UNVERIFIED_EXTERNAL must still be emitted; saw {obs}"
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert not (ev and ev.get("confirmed") is True), f"must not be credited; store={store}"


def test_ac1c_independent_channel_available_no_attest_card(tmp_path, monkeypatch):
    """AC-1c: a NON-money external effect that HAD an independent readback channel
    (a readback_url in the envelope) but wasn't confirmed → NO attest card (attest
    is the fallback, not a shortcut) → stays not-credited."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    obs = []
    cards = _spy_attest_cards(monkeypatch)
    # channel WAS available (readback_url present) but the client echoes nothing.
    tool_parsed = {
        "ok": True,
        "external": {
            "strategy": "api_readback",
            "expected_tokens": ["tok-unconfirmed"],
            "readback_url": "https://api.example.com/rows/1",
            "submit_host": "api.example.com",
        },
    }
    _, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_external_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=_NoopReadbackClient(), spy_obs=obs,
        tool=_nonmoney_tool())

    assert not _attest_cards(cards), (
        "an effect that HAD an independent channel must NOT get an attest card "
        f"(attest is not a shortcut); cards={cards}")
    assert result.get("status") != "suspended_operator_attest"
    store = getattr(ctx, "_external_evidence", {}) or {}
    ev = store.get("1") or store.get(1)
    assert not (ev and ev.get("confirmed") is True), f"must not be credited; store={store}"


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_additive_off_shadow_no_attest_card_byte_identical(tmp_path, monkeypatch, mode):
    """ADDITIVE: in OFF and SHADOW the attest path is byte-identical — the SAME
    non-money, no-channel, unconfirmed effect that parks in ENFORCE must NEVER
    enqueue an attest card nor park for attestation."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", mode)
    cards = _spy_attest_cards(monkeypatch)
    tool_parsed = {"ok": True, "external": {"strategy": "web_assertion",
                                            "observed_text": "done"}}
    _, result, ctx = _drive_live_credit(
        tmp_path, monkeypatch, objectives=[_external_obj()], claim_obj_id=1,
        tool_parsed=tool_parsed, api_client=_NoopReadbackClient(),
        tool=_nonmoney_tool())

    assert not _attest_cards(cards), (
        f"[{mode}] no attest card may be enqueued outside ENFORCE; cards={cards}")
    assert result.get("status") != "suspended_operator_attest", (
        f"[{mode}] must not park for operator attestation; got {result.get('status')}")


# ─────────────────────────────────────────────────────────────────────────────
#  PART B1 — the resume TRIGGER (_dispatch_resume + the reconciler)
# ─────────────────────────────────────────────────────────────────────────────

class _AttestDec:
    def __init__(self, ctx, choice, did="dec_at"):
        self.id, self.context, self.choice = did, ctx, choice


class _AttestSnap:
    def __init__(self):
        self.activity_id = "act_at"
        self.shadow_id = "shadow_at"
        self.sticky_notes = []


class _AttestSup:
    def __init__(self):
        self.submits = []

    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append((activity_id, shadow_id, kw.get("resume_from_execution_id")))


class _AttestVault:
    def save_decision(self, d):
        pass


def _attest_card_ctx(**over):
    ctx = {"kind": "gate", "gate_type": "operator", "kind_marker": "operator_attest",
           "objective_id": 1, "execution_id": "exec_at",
           "chat_submission_id": "sub_at", "effect_class": "net_mutate",
           "is_money_move": False}
    ctx.update(over)
    return ctx


def test_b1_dispatch_resume_attest_stashes_sticky_and_resubmits(monkeypatch, tmp_path):
    """B1: a resolved operator-attest decision (keyed off the kind_marker sibling, NOT
    `kind` which gate.py overwrites to 'gate') stashes an __OPERATOR_ATTEST__ sticky
    carrying the choice + the enqueue-time effect_class, then re-submits with
    resume_from_execution_id."""
    from systemu.runtime import resume_on_decision as rod
    from systemu.runtime import execution_snapshot as es
    rod._handled.clear()
    snap = _AttestSnap()
    written = []
    monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: snap)
    monkeypatch.setattr(es, "write_snapshot", lambda s, data_dir=None: written.append(s))

    sup = _AttestSup()
    ok = rod._dispatch_resume(_AttestDec(_attest_card_ctx(), "Attest occurred"),
                              vault=_AttestVault(), supervisor=sup, data_dir=str(tmp_path))
    assert ok is True
    at = [n for n in snap.sticky_notes if n.startswith("__OPERATOR_ATTEST__::obj_1::")]
    assert at, f"attest sticky must be stashed; notes={snap.sticky_notes}"
    import json
    payload = json.loads(at[0].split("::", 2)[2])
    assert payload["choice"] == "Attest occurred"
    assert payload["effect_class"] == "net_mutate"
    assert written, "the snapshot must be re-written with the sticky"
    assert sup.submits == [("act_at", "shadow_at", "exec_at")]


def test_b1_dispatch_resume_dismiss_still_resumes(monkeypatch, tmp_path):
    """B1: 'Dismiss' also stashes the sticky + resumes (the applier decides not to
    credit) — the attest gate does NOT finalize the activity like a command-gate deny."""
    from systemu.runtime import resume_on_decision as rod
    from systemu.runtime import execution_snapshot as es
    rod._handled.clear()
    snap = _AttestSnap()
    monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: snap)
    monkeypatch.setattr(es, "write_snapshot", lambda s, data_dir=None: None)

    sup = _AttestSup()
    ok = rod._dispatch_resume(_AttestDec(_attest_card_ctx(), "Dismiss"),
                              vault=_AttestVault(), supervisor=sup, data_dir=str(tmp_path))
    assert ok is True
    at = [n for n in snap.sticky_notes if n.startswith("__OPERATOR_ATTEST__::obj_1::")]
    assert at and "Dismiss" in at[0]
    assert sup.submits == [("act_at", "shadow_at", "exec_at")]


def test_b1_plain_operator_gate_without_marker_not_resumed(monkeypatch, tmp_path):
    """B1: a plain operator gate WITHOUT the operator_attest kind_marker must NOT be
    treated as an attest resume (a render-only notify/choice gate resumes nowhere)."""
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    ctx = {"kind": "gate", "gate_type": "operator", "execution_id": "exec_at",
           "chat_submission_id": "sub_at"}
    assert rod._dispatch_resume(_AttestDec(ctx, "Dismiss"), vault=_AttestVault(),
                                supervisor=_AttestSup(), data_dir=str(tmp_path)) is False


def test_b1_reconciler_dispatches_attest(tmp_path):
    """B1: the cross-process reconciler recognizes a resolved attest card (via the
    kind_marker) and re-dispatches it exactly once."""
    from systemu.runtime import resume_on_decision as rod
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions
    from systemu.vault.vault import Vault
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot
    rod._handled.clear()

    for sub in ["scrolls", "activities", "shadow_army", "skills",
                "tools/implementations", "evolutions", "notifications",
                "executions", "decisions"]:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx in ["scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"]:
        (tmp_path / idx / "index.json").write_text("[]", encoding="utf-8")
    vlt = Vault(str(tmp_path))
    data_dir = tmp_path / "data"
    (data_dir / "audit").mkdir(parents=True, exist_ok=True)
    write_snapshot(ExecutionSnapshot(
        execution_id="exec_at", shadow_id="sh_at", scroll_id="sc_at",
        activity_id="act_at", completed_objective_ids=[0]), data_dir=data_dir)

    queue = OperatorDecisionQueue(vlt)
    did = queue.post(
        title="Operator: verify external effect (objective 1)", body="?",
        options=["Dismiss", "Attest occurred"],
        context=_attest_card_ctx(), dedup_key="operator_attest:1")
    queue.resolve(did, choice="Attest occurred")

    sup = _AttestSup()
    n = reconcile_resolved_stuck_decisions(vlt, sup, data_dir=data_dir)
    assert n == 1, "the reconciler must dispatch the resolved attest card"
    assert sup.submits == [("act_at", "sh_at", "exec_at")]


# ─────────────────────────────────────────────────────────────────────────────
#  PART B2 — the resume-start APPLIER (credit round-trip)
# ─────────────────────────────────────────────────────────────────────────────

import json as _json_b2

from test_s3_resume_no_resubmit import (
    _build_entities_objs as _build_r, _redirect_snapshot_io as _redirect_r,
    _external_obj as _external_r, _CaptureAbort,
)


def _attest_sticky(oid, choice, effect_class):
    return ("__OPERATOR_ATTEST__::obj_%d::%s"
            % (oid, _json_b2.dumps({"choice": choice, "effect_class": effect_class,
                                    "is_money_move": effect_class is None})))


def _drive_attest_resume(tmp_path, monkeypatch, *, objectives, graph, completed,
                         sticky):
    """Resume a parked run with an __OPERATOR_ATTEST__ sticky pre-seeded (the REAL
    resume path). Captures the credited objective set right after the resume recredit
    loop (a _resolve_objectives_for_run capture-abort), spies the effectful tool (a
    re-invocation is a silent re-submit — must be 0), and records every persisted
    ExternalEvidence (the applier's verify output). Returns
    ``(credited_set, tool_calls, persisted, result)``."""
    from sharing_on.config import Config
    from systemu.runtime.shadow_runtime import ShadowRuntime
    import systemu.runtime.shadow_runtime as _sr
    from systemu.runtime.execution_snapshot import ExecutionSnapshot, write_snapshot

    vault, shadow, activity = _build_r(tmp_path, objectives)
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    data_dir = tmp_path / "snap_data"
    _redirect_r(monkeypatch, data_dir)

    import systemu.interface.harness_review as _hr
    monkeypatch.setattr(_hr, "surface_harness_request",
                        lambda *a, **k: "card", raising=False)

    runtime = ShadowRuntime(cfg, vault)
    runtime._external_api_client = _NoopReadbackClient()   # neutralise prod client

    tool_calls = {"n": 0}

    async def _handle(decision, tools, context, current_ab, dry_run, **kw):
        tool_calls["n"] += 1
        from systemu.runtime.tool_sandbox import ToolResult
        return ToolResult(success=True, parsed={"ok": True})
    monkeypatch.setattr(runtime, "_handle_tool_call", _handle)

    persisted = []
    _orig_persist = _sr._persist_external_evidence

    def _persist_spy(context, evidence):
        persisted.append(evidence)
        return _orig_persist(context, evidence)
    monkeypatch.setattr(_sr, "_persist_external_evidence", _persist_spy)

    captured = {}

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

    snap = ExecutionSnapshot(
        execution_id="exec_attest", shadow_id=shadow.id, scroll_id="scroll_s3r",
        activity_id=activity.id, iteration=1,
        completed_objective_ids=list(completed), objective_graph=graph,
        next_objective_id=99, external_evidence={}, sticky_notes=[sticky])
    write_snapshot(snap, data_dir=data_dir)

    decisions = [{"action": "FAIL", "reason": "unreachable — capture unwinds first"}]
    result = None
    with patch("systemu.runtime.shadow_runtime.llm_call_json", side_effect=decisions), \
         patch("systemu.runtime.shadow_runtime._dispatch_refinery"):
        try:
            result = asyncio.run(runtime.execute(
                shadow, activity, resume_from_execution_id="exec_attest"))
        except _CaptureAbort:
            pass
    return captured.get("completed", set()), tool_calls["n"], persisted, result


def _confirmed_for(persisted, oid):
    return [e for e in persisted
            if getattr(e, "objective_id", None) == oid
            and getattr(e, "confirmed", None) is True]


def test_ac2_attest_credits_on_resume_tool_not_reinvoked(tmp_path, monkeypatch):
    """AC-2: a parked NON-money run + operator 'Attest occurred' + the
    __OPERATOR_ATTEST__ sticky → resume CREDITS the objective (via the S4 short-
    circuit reading the applier's persisted confirmed bit) AND the effectful tool is
    NEVER re-invoked. The credit comes from a REAL verify(operator_attest) call — no
    synthetic evidence dict."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    from systemu.core.models import Objective
    graph = [_external_r(id=1, depends_on=[]),
             Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[])]
    objectives = [_external_r(id=1, depends_on=[]),
                  Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[])]

    credited, tool_calls, persisted, _ = _drive_attest_resume(
        tmp_path, monkeypatch, objectives=objectives, graph=graph, completed=[2],
        sticky=_attest_sticky(1, "Attest occurred", "net_mutate"))

    assert 1 in credited, (
        f"an attested non-money external objective must be CREDITED on resume; "
        f"credited={credited}")
    assert tool_calls == 0, (
        f"the effectful tool must NOT be re-invoked on an attest resume; "
        f"called {tool_calls}x")
    assert _confirmed_for(persisted, 1), (
        "the applier must persist a CONFIRMED operator_attest evidence for obj 1; "
        f"persisted={[getattr(e,'method',None) for e in persisted]}")
    assert _confirmed_for(persisted, 1)[0].method == "operator_attest", (
        "the persisted attest evidence must carry method=operator_attest (provenance)")


def test_ac2b_money_move_attest_cannot_credit(tmp_path, monkeypatch):
    """AC-2b: a MONEY-MOVE objective with an attest sticky → the applier's fail-closed
    money-move skip / verify's money-move demote → NOT credited. Proves the credit-time
    gate independent of the enqueue gate."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    from systemu.core.models import Objective
    money_obj = lambda: _external_r(  # noqa: E731
        id=1, goal="pay the $500 invoice via the checkout API",
        success_criteria="payment confirmed", depends_on=[])
    graph = [money_obj(),
             Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[])]
    objectives = [money_obj(),
                  Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[])]

    credited, tool_calls, persisted, result = _drive_attest_resume(
        tmp_path, monkeypatch, objectives=objectives, graph=graph, completed=[2],
        sticky=_attest_sticky(1, "Attest occurred", "net_mutate"))

    assert 1 not in credited, f"a money-move must NEVER credit via attest; credited={credited}"
    assert not _confirmed_for(persisted, 1), (
        "no confirmed evidence may be persisted for a money-move attest; "
        f"persisted={[getattr(e,'method',None) for e in persisted]}")
    assert result is None or str(result.get("status", "")) != "success", (
        f"a money-move attest must not finalize success; got {result}")
    assert tool_calls == 0


def test_ac3_dismiss_does_not_credit(tmp_path, monkeypatch):
    """AC-3: 'Dismiss' (the safe default) → no verify → the objective stays
    uncredited (and the effectful tool is not re-invoked)."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    from systemu.core.models import Objective
    graph = [_external_r(id=1, depends_on=[]),
             Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[])]
    objectives = [_external_r(id=1, depends_on=[]),
                  Objective(id=2, goal="prep", success_criteria="prepped", depends_on=[])]

    credited, tool_calls, persisted, result = _drive_attest_resume(
        tmp_path, monkeypatch, objectives=objectives, graph=graph, completed=[2],
        sticky=_attest_sticky(1, "Dismiss", "net_mutate"))

    assert 1 not in credited, f"'Dismiss' must not credit; credited={credited}"
    assert not _confirmed_for(persisted, 1), (
        "'Dismiss' must not persist any confirmed evidence; "
        f"persisted={[getattr(e,'method',None) for e in persisted]}")
    assert result is None or str(result.get("status", "")) != "success"
    assert tool_calls == 0


# ─────────────────────────────────────────────────────────────────────────────
#  PART B2 (empty-completed) — the double-submit defect: an attested external
#  objective with NO completed sibling (snap.completed_objective_ids == [])
# ─────────────────────────────────────────────────────────────────────────────

def test_ac2c_attest_credits_empty_completed_no_double_submit(tmp_path, monkeypatch):
    """AC-2c (double-submit FIX): a SINGLE parked NON-money external objective with
    NO completed sibling (completed=[]) + operator 'Attest occurred' → resume still
    CREDITS the objective, so the run does not fall through and RE-DRIVE the effectful
    submit.

    Pre-fix this FAILED (uncredited): the S4 external-recredit short-circuit that turns
    the applier's persisted confirmed bit INTO a credit lived inside the
    ``if use_objectives and snap.completed_objective_ids:`` guard, so an EMPTY completed
    set skipped it — the objective was never credited. Yet the applier had already set
    ``_read_external_ok`` True, so the resubmit guard EXCLUDED the objective → the run
    neither parked nor credited and fell through to the main loop → the LLM re-drove the
    effectful tool = a double-submit. Post-fix the hoisted recredit credits it here."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    graph = [_external_r(id=1, depends_on=[])]
    objectives = [_external_r(id=1, depends_on=[])]

    credited, tool_calls, persisted, result = _drive_attest_resume(
        tmp_path, monkeypatch, objectives=objectives, graph=graph, completed=[],
        sticky=_attest_sticky(1, "Attest occurred", "net_mutate"))

    assert 1 in credited, (
        "an attested non-money external objective with NO completed sibling must be "
        "CREDITED on resume — an empty completed set must not skip the external "
        f"recredit short-circuit; credited={credited}")
    assert tool_calls == 0, (
        f"the effectful tool must NOT be re-invoked on an attest resume; "
        f"called {tool_calls}x")
    assert _confirmed_for(persisted, 1), (
        "the applier must persist a CONFIRMED operator_attest evidence for obj 1; "
        f"persisted={[getattr(e,'method',None) for e in persisted]}")
    # It reached the main-loop objective resolution (capture-abort ⇒ result None)
    # rather than parking — and, post-fix, did so WITH obj 1 credited (so obj 1 is
    # not re-driven). Pre-fix it ALSO reached here but with obj 1 UNcredited (proving
    # the fall-through / re-drive double-submit); the credit assertion above is the
    # discriminating RED→GREEN signal.
    assert result is None, (
        f"the run should reach _resolve_objectives_for_run (capture-abort), not park; "
        f"got result={result}")


def test_ac2b_empty_completed_money_move_attest_not_credited(tmp_path, monkeypatch):
    """AC-2b (empty-completed variant): the empty-completed recredit path must NOT
    accidentally credit a MONEY-MOVE. The applier persists NO confirmed bit for a
    money-move (fail-closed), so ``_read_external_ok`` stays False → the hoisted
    external recredit finds nothing to credit → not credited; the resubmit guard then
    PARKS it (never a silent re-submit)."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    _money = lambda: _external_r(  # noqa: E731
        id=1, goal="pay the $500 invoice via the checkout API",
        success_criteria="payment confirmed", depends_on=[])
    graph = [_money()]
    objectives = [_money()]

    credited, tool_calls, persisted, result = _drive_attest_resume(
        tmp_path, monkeypatch, objectives=objectives, graph=graph, completed=[],
        sticky=_attest_sticky(1, "Attest occurred", "net_mutate"))

    assert 1 not in credited, (
        f"a money-move must NEVER credit via the empty-completed recredit; "
        f"credited={credited}")
    assert not _confirmed_for(persisted, 1), (
        "no confirmed evidence may be persisted for a money-move attest; "
        f"persisted={[getattr(e,'method',None) for e in persisted]}")
    assert tool_calls == 0
    assert result is not None and str(result.get("status", "")) == "suspended_external_resubmit", (
        f"an unconfirmed money-move external objective must still PARK; got {result}")


def test_ac3_empty_completed_dismiss_not_credited_parks(tmp_path, monkeypatch):
    """AC-3 (empty-completed variant): 'Dismiss' on an empty-completed resume → no
    confirmed bit → not credited; the resubmit guard PARKS (a genuinely-unconfirmed
    external objective is still protected from a silent re-submit)."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    graph = [_external_r(id=1, depends_on=[])]
    objectives = [_external_r(id=1, depends_on=[])]

    credited, tool_calls, persisted, result = _drive_attest_resume(
        tmp_path, monkeypatch, objectives=objectives, graph=graph, completed=[],
        sticky=_attest_sticky(1, "Dismiss", "net_mutate"))

    assert 1 not in credited, (
        f"'Dismiss' must not credit on an empty-completed resume; credited={credited}")
    assert not _confirmed_for(persisted, 1), (
        "'Dismiss' must not persist any confirmed evidence")
    assert tool_calls == 0
    assert result is not None and str(result.get("status", "")) == "suspended_external_resubmit", (
        f"a genuinely-unconfirmed external objective must still PARK via the resubmit "
        f"guard; got {result}")


def test_empty_completed_no_attest_external_still_parks(tmp_path, monkeypatch):
    """Non-regression: an empty-completed resume of an unconfirmed external objective
    with NO operator-attest sticky at all still PARKS via the resubmit guard — the fix
    only credits CONFIRMED externals; it does not weaken the guard for unconfirmed
    ones (no fall-through / re-drive)."""
    monkeypatch.setenv("SYSTEMU_S4_STAMP", "enforce")
    graph = [_external_r(id=1, depends_on=[])]
    objectives = [_external_r(id=1, depends_on=[])]

    credited, tool_calls, persisted, result = _drive_attest_resume(
        tmp_path, monkeypatch, objectives=objectives, graph=graph, completed=[],
        sticky="__UNRELATED__::noise")

    assert 1 not in credited, f"an unconfirmed external must not credit; credited={credited}"
    assert not _confirmed_for(persisted, 1)
    assert tool_calls == 0
    assert result is not None and str(result.get("status", "")) == "suspended_external_resubmit", (
        f"an unconfirmed external objective must PARK; got {result}")


def test_gate_c_verify_demotes_money_move_operator_attest():
    """Gate (c): ExternalVerifier.verify(operator_attest) confirms a NON-money effect
    but its own money-move gate demotes a money-move to confirmed=False — the credit-
    ENGINE-level defense, independent of the enqueue + applier gates."""
    from types import SimpleNamespace
    from systemu.runtime.external_verifier import ExternalVerifier

    money = SimpleNamespace(id=1, goal="pay the $500 invoice", success_criteria="paid",
                            requires_external_verification=True, effect_tags=[])
    ev = ExternalVerifier().verify(
        money, None, {"strategy": "operator_attest", "attested": True})
    assert ev.confirmed is False, "operator_attest can NEVER confirm a money-move (gate c)"

    nonmoney = SimpleNamespace(id=2, goal="post the row to the api",
                               success_criteria="row present",
                               requires_external_verification=True, effect_tags=[])
    ev2 = ExternalVerifier().verify(
        nonmoney, "net_mutate", {"strategy": "operator_attest", "attested": True})
    assert ev2.confirmed is True and ev2.method == "operator_attest", (
        "operator_attest with a known non-money effect_class must confirm a non-money "
        f"effect; got confirmed={ev2.confirmed} method={ev2.method}")
