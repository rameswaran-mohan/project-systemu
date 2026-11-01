"""P28 (GAP 2) -- a typed "Approve once" on an R1 MCP card is bound to the card's
expiry and to the run that raised it.

The defect: the typed one-shot was honoured once by WHICHEVER lane made that exact
call next (phrase + arguments checked, then consumed) -- including a re-run of a
stopped task -- with no time bound and no run bound, so an identical payment much
later, from an unrelated run, could cash it.

THE RULING pinned here (``systemu.runtime.run_lineage``), against the REAL MCP gate
(``dispatch._gate_mcp_call``) posting REAL cards into a real vault, resolved through
the dashboard Inbox's own server-side path (``inbox.resolve_pending_gate``) with the
typed phrase:

  (i)  the card carries its own expiry (``expires_at``, stamped and shown); once it
       has passed, the typed approval is void -- refused, nothing runs;
  (ii) the card carries the run that raised it; the approval is honoured by that
       run, its direct RESUME (``resume_from_execution_id`` naming it, or the same
       run tree) or its direct RE-RUN (the single-use stopped-run handoff the next
       run of the task claims). Anything else is refused with "This approval was
       for <run>; ask again." -- and the approval is NOT spent: it stays for its own
       run;
  (iii) consumed atomically once (the existing peek / compare / consume).

The end-to-end re-run witness through the REAL ``ShadowRuntime`` (the run stops at
the R1 card, the operator types the phrase, the task runs again and the payment's
ledger row carries the approve-once pass) is
``tests/test_gdemo_v0_acceptance.py::test_gdemo_money_move_stays_claimed``.

Mutations (each checked; each turns the named tests red):
  * the one-shot ignores the card's TTL
      -> test_a_typed_approval_is_void_after_the_cards_ttl
  * the one-shot ignores the run lineage
      -> test_an_unrelated_run_with_identical_args_is_refused,
         test_the_chat_lane_of_an_unrelated_run_neither_runs_nor_spends_it,
         test_a_later_run_of_the_same_task_is_not_its_direct_rerun
  * a v2 handler runs outside the run's context
      -> test_a_v2_handler_runs_inside_the_runs_context,
         test_gdemo_v0_acceptance::test_gdemo_money_move_stays_claimed
  * a run stopping at a card leaves no handoff / the next run claims none
      -> test_a_stopped_run_hands_its_id_to_the_next_run_of_the_task,
         test_gdemo_v0_acceptance::test_gdemo_money_move_stays_claimed (claim only)
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from systemu.approval.exceptions import PendingOperatorDecision

SERVER = "srv"
TOOL = "transfer_funds"
PARAMS = {"to_account": "acct-9", "amount": 500}


@pytest.fixture
def env(tmp_path, monkeypatch):
    import systemu.runtime.command_approvals as ca
    from systemu.runtime.mcp import connections as conn
    from systemu.vault.vault import Vault
    v = Vault(str(tmp_path / "vault"))
    ca.reset_default_store_for_tests()
    ca.init_default_store(tmp_path / "approvals")
    conn.set_tool_enabled(v, SERVER, TOOL, True, description="d",
                          schema={"type": "object", "properties": {}}, annotations={})
    conn.set_tool_hash(v, SERVER, TOOL, "pinned")
    events = []
    import systemu.interface.notifications as notifications
    monkeypatch.setattr(notifications, "log_event",
                        lambda *a, **k: events.append(a))
    try:
        yield type("Env", (), {"vault": v, "events": events})
    finally:
        ca.reset_default_store_for_tests()


def _gate(v, *, run, lineage=None, resolved_dedup=None, params=PARAMS):
    """One call through the REAL MCP gate, made by run ``run`` (its per-run id, as
    every lane passes it) with the run lineage ``lineage`` in context."""
    from systemu.runtime import run_lineage
    from systemu.runtime.mcp import dispatch
    token = run_lineage.set_run_lineage(lineage) if lineage is not None else None
    try:
        return dispatch._gate_mcp_call(SERVER, TOOL, dict(params), vault=v, config=None,
                                       session_id=run, resolved_dedup=resolved_dedup)
    finally:
        run_lineage.reset_run_lineage(token)


def _card(v, *, run, lineage=None):
    with pytest.raises(PendingOperatorDecision) as ei:
        _gate(v, run=run, lineage=lineage)
    return ei.value, v.get_decision(ei.value.decision_id)


def _approve_typed(v, dec):
    from systemu.interface.command.inbox import resolve_pending_gate
    res = resolve_pending_gate(dec.id, choice="Approve once", vault=v,
                               typed_confirm="500")
    assert not (res.data or {}).get("typed_confirm_refused"), res.summary
    return res


# ─────────────────────────────────────────────────────────────────────────────
# The card carries its binding, and says it
# ─────────────────────────────────────────────────────────────────────────────

def test_the_r1_card_carries_its_run_and_expiry(env):
    from systemu.runtime import run_lineage
    pend, dec = _card(env.vault, run="exec_A",
                      lineage=run_lineage.RunLineage(root_execution_id="exec_A"))
    b = dec.context[run_lineage.ONE_SHOT_BINDING_KEY]
    assert b["run_id"] == "exec_A" and b["root_execution_id"] == "exec_A"
    expires = run_lineage._parse_utc(b["expires_at"])
    left = (expires - run_lineage.utcnow()).total_seconds()
    assert 0 < left <= run_lineage.TYPED_APPROVAL_TTL_SECONDS
    assert "run exec_A" in dec.body and run_lineage._when(expires) in dec.body
    assert dec.body.isascii()


def test_the_run_that_raised_the_card_runs_it_once(env):
    _pend, dec = _card(env.vault, run="exec_A")
    _approve_typed(env.vault, dec)
    rec = _gate(env.vault, run="exec_A")
    assert rec["passed_via"] == "approve_once" and rec["decision_id"] == dec.id
    assert env.vault.get_decision(dec.id).status == "consumed"
    with pytest.raises(PendingOperatorDecision):          # one typed approval, one call
        _gate(env.vault, run="exec_A")


# ─────────────────────────────────────────────────────────────────────────────
# (ii) lineage
# ─────────────────────────────────────────────────────────────────────────────

def test_an_unrelated_run_with_identical_args_is_refused(env):
    """Run B makes the identical payment: refused, with the sentence, a fresh card,
    and the approval left UNSPENT for run A -- which then still runs it."""
    _pend, dec = _card(env.vault, run="exec_A")
    _approve_typed(env.vault, dec)
    with pytest.raises(PendingOperatorDecision) as ei:
        _gate(env.vault, run="exec_B")
    assert "This approval was for run exec_A; ask again." in str(ei.value)
    fresh = env.vault.get_decision(ei.value.decision_id)
    assert fresh.id != dec.id
    assert "This approval was for run exec_A; ask again." in fresh.body
    assert fresh.context["one_shot_binding"]["run_id"] == "exec_B"
    assert any("This approval was for run exec_A; ask again." in a[2]
               for a in env.events), env.events
    assert env.vault.get_decision(dec.id).status == "resolved", "B spent A's approval"
    rec = _gate(env.vault, run="exec_A")
    assert rec["passed_via"] == "approve_once" and rec["decision_id"] == dec.id


def test_no_run_at_all_is_refused(env):
    """An absent run id matches nothing -- not even another absent one."""
    _pend, dec = _card(env.vault, run="")
    assert dec.context["one_shot_binding"]["run_id"] == ""
    _approve_typed(env.vault, dec)
    with pytest.raises(PendingOperatorDecision) as ei:
        _gate(env.vault, run="")
    assert "This approval was for an unrecorded run; ask again." in str(ei.value)


def test_the_chat_lane_of_an_unrelated_run_neither_runs_nor_spends_it(env):
    """Run B's chat lane re-calls with the card's dedup (it polled the shared,
    params-independent key and saw A's resolution): refused, and A's approval is
    not consumed by B's lane."""
    pend, dec = _card(env.vault, run="exec_A")
    _approve_typed(env.vault, dec)
    with pytest.raises(PendingOperatorDecision):
        _gate(env.vault, run="quick_B", resolved_dedup=pend.dedup_key)
    assert env.vault.get_decision(dec.id).status == "resolved"
    rec = _gate(env.vault, run="exec_A", resolved_dedup=pend.dedup_key)
    assert rec["passed_via"] == "approve_once" and rec["decision_id"] == dec.id


def test_the_direct_resume_is_accepted(env):
    """A run started with ``resume_from_execution_id`` naming the run that raised
    the card (the resume rail's re-dispatch) runs it."""
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A",
                       lineage=run_lineage.RunLineage(root_execution_id="exec_A"))
    _approve_typed(env.vault, dec)
    rec = _gate(env.vault, run="exec_A2", lineage=run_lineage.RunLineage(
        root_execution_id="exec_A2", resume_from_execution_id="exec_A"))
    assert rec["passed_via"] == "approve_once" and rec["decision_id"] == dec.id


def test_the_same_run_tree_is_accepted(env):
    """A resume that restored the run tree's root from the snapshot (or a sub-agent
    of the run) is the same run tree."""
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A",
                       lineage=run_lineage.RunLineage(root_execution_id="exec_R"))
    _approve_typed(env.vault, dec)
    rec = _gate(env.vault, run="exec_A3",
                lineage=run_lineage.RunLineage(root_execution_id="exec_R"))
    assert rec["passed_via"] == "approve_once"


def test_the_direct_rerun_is_accepted_through_the_stopped_run_handoff(env):
    """The run stops at the card and leaves the single-use handoff; the NEXT run of
    the same task claims it at start and carries the stopped run's id forward --
    that re-run's identical call runs on the approval."""
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A",
                       lineage=run_lineage.RunLineage(root_execution_id="exec_A"))
    assert run_lineage.record_stopped_run(env.vault, activity_id="act_1",
                                          execution_id="exec_A")
    _approve_typed(env.vault, dec)
    continues = run_lineage.claim_stopped_run(env.vault, "act_1")
    assert continues == ("exec_A",)
    rec = _gate(env.vault, run="exec_Z", lineage=run_lineage.RunLineage(
        root_execution_id="exec_Z", continues=continues))
    assert rec["passed_via"] == "approve_once" and rec["decision_id"] == dec.id


def test_a_later_run_of_the_same_task_is_not_its_direct_rerun(env):
    """The handoff is taken ONCE: the run after the direct re-run carries nothing,
    so an identical payment from it is refused."""
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A")
    run_lineage.record_stopped_run(env.vault, activity_id="act_1", execution_id="exec_A")
    _approve_typed(env.vault, dec)
    first = run_lineage.claim_stopped_run(env.vault, "act_1")   # the direct re-run
    assert first == ("exec_A",)
    later = run_lineage.claim_stopped_run(env.vault, "act_1")   # a later run
    assert later == ()
    with pytest.raises(PendingOperatorDecision) as ei:
        _gate(env.vault, run="exec_Later", lineage=run_lineage.RunLineage(
            root_execution_id="exec_Later", continues=later))
    assert "This approval was for run exec_A; ask again." in str(ei.value)


def test_a_rerun_of_a_rerun_that_stopped_at_the_same_card_is_accepted(env):
    """The operator re-ran the task before approving: that re-run stopped at the
    SAME pending card. Its own re-run carries the whole chain forward."""
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A")
    run_lineage.record_stopped_run(env.vault, activity_id="act_1", execution_id="exec_A")
    chain = run_lineage.claim_stopped_run(env.vault, "act_1")
    with pytest.raises(PendingOperatorDecision) as ei:      # E2 hits the pending card
        _gate(env.vault, run="exec_E2",
              lineage=run_lineage.RunLineage(continues=chain))
    assert ei.value.decision_id == dec.id
    run_lineage.record_stopped_run(env.vault, activity_id="act_1", execution_id="exec_E2",
                                   lineage=run_lineage.RunLineage(continues=chain))
    _approve_typed(env.vault, dec)
    chain3 = run_lineage.claim_stopped_run(env.vault, "act_1")
    assert chain3 == ("exec_E2", "exec_A")
    rec = _gate(env.vault, run="exec_E3", lineage=run_lineage.RunLineage(continues=chain3))
    assert rec["passed_via"] == "approve_once" and rec["decision_id"] == dec.id


# ─────────────────────────────────────────────────────────────────────────────
# (i) the card's own expiry
# ─────────────────────────────────────────────────────────────────────────────

def _move_clock(monkeypatch, seconds):
    from systemu.runtime import run_lineage
    real = run_lineage.utcnow()
    monkeypatch.setattr(run_lineage, "utcnow", lambda: real + timedelta(seconds=seconds))


def test_a_typed_approval_is_void_after_the_cards_ttl(env, monkeypatch):
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A")
    _approve_typed(env.vault, dec)
    _move_clock(monkeypatch, run_lineage.TYPED_APPROVAL_TTL_SECONDS + 5)
    with pytest.raises(PendingOperatorDecision) as ei:
        _gate(env.vault, run="exec_A")
    assert "This approval was for run exec_A and expired at" in str(ei.value)
    assert "ask again." in str(ei.value)
    assert env.vault.get_decision(dec.id).status == "resolved"   # not consumed


def test_inside_the_ttl_it_still_runs(env, monkeypatch):
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A")
    _approve_typed(env.vault, dec)
    _move_clock(monkeypatch, run_lineage.TYPED_APPROVAL_TTL_SECONDS - 60)
    assert _gate(env.vault, run="exec_A")["passed_via"] == "approve_once"


def test_an_expired_pending_card_is_withdrawn_and_a_fresh_one_posted(env, monkeypatch):
    """A PENDING card past its expiry is never re-surfaced (its approval would be
    void): the next call withdraws it and posts a fresh card with a fresh expiry."""
    from systemu.runtime import run_lineage
    pend, dec = _card(env.vault, run="exec_A")
    _move_clock(monkeypatch, run_lineage.TYPED_APPROVAL_TTL_SECONDS + 5)
    with pytest.raises(PendingOperatorDecision) as ei:
        _gate(env.vault, run="exec_A")
    assert ei.value.decision_id != dec.id
    assert env.vault.get_decision(dec.id).status == "expired"
    fresh = env.vault.get_decision(ei.value.decision_id)
    assert run_lineage._parse_utc(fresh.context["one_shot_binding"]["expires_at"]) > (
        run_lineage.utcnow())


def test_a_card_from_before_the_binding_is_refused(env):
    """A typed approval on a card an older build posted (no binding) is void: it
    asks again, it never widens."""
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A")
    _approve_typed(env.vault, dec)
    stored = env.vault.get_decision(dec.id)
    stored.context.pop(run_lineage.ONE_SHOT_BINDING_KEY)
    env.vault.save_decision(stored)
    with pytest.raises(PendingOperatorDecision) as ei:
        _gate(env.vault, run="exec_A")
    assert "This approval was for an unrecorded run; ask again." in str(ei.value)


# ─────────────────────────────────────────────────────────────────────────────
# What the operator reads on approving
# ─────────────────────────────────────────────────────────────────────────────

def test_the_approve_notice_names_the_run_and_the_expiry(env):
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A")
    res = _approve_typed(env.vault, dec)
    b = env.vault.get_decision(dec.id).context["one_shot_binding"]
    assert "run exec_A" in res.summary
    assert run_lineage._when(run_lineage._parse_utc(b["expires_at"])) in res.summary
    assert "Any other run asks again" in res.summary
    assert res.data["notice_kind"] == "positive"


def test_approving_a_card_without_a_binding_never_claims_it_runs(env):
    """DEC-34: an older build's R1 card (no run, no expiry) is refused by the gate,
    so approving it must not be reported as "the next call runs"."""
    from systemu.interface.command.inbox import mcp_gate_decision_notice
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A")
    stored = env.vault.get_decision(dec.id)
    stored.context.pop(run_lineage.ONE_SHOT_BINDING_KEY)
    summary, kind = mcp_gate_decision_notice(stored, "approve once", vault=env.vault)
    assert "not run on it" in summary and "asks again" in summary, summary
    assert "the next call with them runs" not in summary
    assert kind == "info"


def test_approving_an_expired_card_says_nothing_will_run(env, monkeypatch):
    from systemu.runtime import run_lineage
    _pend, dec = _card(env.vault, run="exec_A")
    _move_clock(monkeypatch, run_lineage.TYPED_APPROVAL_TTL_SECONDS + 5)
    res = _approve_typed(env.vault, dec)
    assert "expired" in res.summary and "asks again" in res.summary
    assert res.data["notice_kind"] == "info"


# ─────────────────────────────────────────────────────────────────────────────
# The handoff store and the run context carrier
# ─────────────────────────────────────────────────────────────────────────────

def test_the_handoff_is_single_use_bounded_and_fails_closed(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from systemu.runtime import run_lineage
    v = SimpleNamespace(root=str(tmp_path))
    assert run_lineage.claim_stopped_run(v, "act") == ()
    assert run_lineage.record_stopped_run(v, activity_id="act", execution_id="e1")
    assert run_lineage.record_stopped_run(v, activity_id="other", execution_id="e9")
    assert run_lineage.claim_stopped_run(v, "act") == ("e1",)
    assert run_lineage.claim_stopped_run(v, "act") == ()
    # older than the approval TTL: nothing it could unlock is valid any more
    run_lineage.record_stopped_run(v, activity_id="act", execution_id="e2")
    _move_clock(monkeypatch, run_lineage.TYPED_APPROVAL_TTL_SECONDS + 5)
    assert run_lineage.claim_stopped_run(v, "act") == ()
    # unreadable store / no vault root / blank ids: nothing, never a raise
    (tmp_path / "stopped_runs.json").write_text("{not json", encoding="utf-8")
    assert run_lineage.claim_stopped_run(v, "other") == ()
    assert run_lineage.claim_stopped_run(SimpleNamespace(), "act") == ()
    assert not run_lineage.record_stopped_run(v, activity_id="", execution_id="e")
    # the next record replaces the damaged store whole
    assert run_lineage.record_stopped_run(v, activity_id="x", execution_id="e3")
    stored = json.loads((tmp_path / "stopped_runs.json").read_text("utf-8"))
    assert list(stored["activities"]) == ["x"]


def test_a_stopped_run_hands_its_id_to_the_next_run_of_the_task(tmp_path, monkeypatch):
    """Through the REAL ``ShadowRuntime.execute``: a run that stops at an operator
    card (``PendingOperatorDecision`` unwinds it) leaves the handoff; the NEXT run
    of the same task starts with that run's id in its lineage; the run after that
    carries nothing; and no run's lineage outlives it."""
    from types import SimpleNamespace
    from sharing_on.config import Config
    from systemu.runtime import run_lineage
    from systemu.runtime.chat_submission_ctx import current_execution_id
    from systemu.runtime.shadow_runtime import ShadowRuntime
    from systemu.vault.vault import Vault
    v = Vault(str(tmp_path / "vault"))
    cfg = Config()
    cfg.vault_dir = str(tmp_path / "vault")
    cfg.output_dir = str(tmp_path / "out")
    rt = ShadowRuntime(cfg, v)
    seen = []

    def _get_scroll(_sid):                      # the first thing the run loads
        seen.append((current_execution_id(), run_lineage.current_run_lineage()))
        if len(seen) == 1:
            raise PendingOperatorDecision(decision_id="dec_p28", dedup_key="mcp:s:t",
                                          options=["Deny", "Approve once"])
        raise RuntimeError("p28: stop the run here")

    monkeypatch.setattr(v, "get_scroll", _get_scroll)
    shadow = SimpleNamespace(id="sh_p28", name="p28")
    activity = SimpleNamespace(id="act_p28", name="p28", scroll_id="sc", origin=None,
                               required_tool_ids=[], required_skill_ids=[])

    def _run():
        try:
            return asyncio.run(rt.execute(shadow, activity))
        except RuntimeError:
            return None

    with pytest.raises(PendingOperatorDecision):
        asyncio.run(rt.execute(shadow, activity))
    _run()
    _run()
    (stopped, lin1), (_e2, lin2), (_e3, lin3) = seen
    assert stopped and lin1.continues == ()
    assert lin2.continues == (stopped,), "the direct re-run did not carry the id"
    assert lin3.continues == (), "a later run of the task carried it too"
    assert run_lineage.current_run_lineage() == run_lineage.RunLineage()


def test_the_handoff_store_has_its_known_writers_and_a_conc_map_row():
    """DEC-10: the stopped-run handoff is written only by ``run_lineage`` and called
    only from ``ShadowRuntime.execute``; CONC-MAP documents it."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    callers = {}
    for call in ("record_stopped_run(", "claim_stopped_run("):
        callers[call] = sorted(
            p.relative_to(root / "systemu").as_posix()
            for p in (root / "systemu").rglob("*.py")
            if p.name != "run_lineage.py"
            and any(call in ln and not ln.lstrip().startswith(("#", "def "))
                    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()))
    assert callers == {"record_stopped_run(": ["runtime/shadow_runtime.py"],
                       "claim_stopped_run(": ["runtime/shadow_runtime.py"]}, callers
    conc = (root / "docs" / "CONC-MAP.md").read_text(encoding="utf-8")
    assert "**P28 stopped-run handoff** `<root>/stopped_runs.json`" in conc


def test_a_v2_handler_runs_inside_the_runs_context():
    """``run_in_executor`` does not carry contextvars to its thread; the sandbox now
    runs a sync v2 handler (every workflow-lane MCP call) inside a copy of the run's
    context, so the gate sees the run that made the call."""
    from systemu.runtime import run_lineage
    from systemu.runtime.chat_submission_ctx import current_execution_id, set_execution_id
    from systemu.runtime.mcp_run_ctx import current_mcp_session_id, set_mcp_session_id
    from systemu.runtime.tool_registry_v2 import registry
    from systemu.runtime.tool_sandbox import ToolSandbox

    seen = {}

    def _handler(**kw):
        seen.update(eid=current_execution_id(), sid=current_mcp_session_id(),
                    lin=run_lineage.current_run_lineage())
        return {"success": True}

    registry.register(name="p28_ctx_probe", toolset="test", schema={}, handler=_handler)

    async def _run():
        set_execution_id("exec_ctx")
        set_mcp_session_id("exec_ctx")
        run_lineage.set_run_lineage(run_lineage.RunLineage(root_execution_id="exec_root"))
        sb = ToolSandbox(vault_root=".", vault=None)
        return await sb.execute("p28_ctx_probe", {})

    try:
        out = asyncio.run(_run())
    finally:
        registry.unregister("p28_ctx_probe")
    assert out.get("success") is True, out
    assert seen == {"eid": "exec_ctx", "sid": "exec_ctx",
                    "lin": run_lineage.RunLineage(root_execution_id="exec_root")}
