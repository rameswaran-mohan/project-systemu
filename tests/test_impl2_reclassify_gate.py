"""IMPL-2 Steps 3-6 — the gate consumes a reclassification, and the cards it posts.

THE TRACE THIS FILE ENCODES (``test_the_end_to_end_reclassify_trace``):

    a DENY-band call posts a DENY card
      → operator reclassifies (typed-confirm)
      → the resumed run posts a REQUIRE_APPROVAL FOLLOW-UP card, not a DENY,
        and does NOT run the call
      → operator clicks "Approve once" on the follow-up
      → the resumed run bypasses via the single-use bridge, spends the
        reclassification, and runs the call EXACTLY ONCE
      → the next identical call DENYs again.

Everything else here pins the three ways that trace could go SILENT — i.e. run the
call without the operator ever seeing a card on the new classification. A gate that
fails silent is worse than one that fails noisy, so each is pinned separately:

  1. a STANDING allow granted in the benign era short-circuiting the follow-up card;
  2. the resolved-dedup bypass treating a decision from SOME OTHER card as an approval
     of this one — either the reclassify click itself, or (the round-3 CRITICAL) a
     stale "Approve once" left behind by an unrelated call on the same
     params-independent dedup key. Both are refused here; the end-to-end reproduction
     of the latter through production code lives in
     ``tests/test_impl2_resolved_dedup_scope.py``;
  3. the reclassification outliving the one call it was reasoned about.

While a reclassification is pending there are exactly TWO ways past the gate, and both
are scope-bound to the FOLLOW-UP CARD for that class and those params: the single-use
``resume_pending`` bridge (workflow lane) and the resolved-dedup decision (chat lane).
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.models import Tool, ToolType
from systemu.interface.command.gate import RECLASSIFY_OPTION, GateDescriptor
from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature
from systemu.runtime.tool_sandbox import args_fingerprint

_BODY = "def run():\n    return {'success': True}\n"

# A destructive param set — the params-dependent half of the DENY band. The key is
# deliberately NOT one of tool_sandbox._PATH_PARAM_KEYS: those are rewritten by the
# output-dir redirect BEFORE the gate runs, so a test computing the fingerprint from
# the literal dict would disagree with the gate. The redirect interaction gets its own
# test (test_the_fingerprint_describes_the_call_that_will_actually_run).
NASTY = {"target": "/data/quarterly_report", "flags": "--force"}
BENIGN = {"target": "/data/quarterly_report"}
# A SECOND destructive param set on the same tool. Same signature (the signature is
# params-INDEPENDENT), different call — the substitution vector.
OTHER_NASTY = {"target": "/etc/production_secrets", "flags": "--force"}


def _reclassify(store, sig, cls, params):
    """Record a reclassification the way production does — scoped to the exact call
    the operator was looking at. Tests must not be able to mint a record that
    production could not, or they stop testing production."""
    ok = store.mark_reclassified(sig, cls, args_fingerprint=args_fingerprint(params))
    assert ok is True, f"the fixture itself failed to record {cls!r}"
    return ok


def _peek(store, sig, params):
    return store.peek_reclassified(sig, args_fingerprint=args_fingerprint(params))


def _bridges(store):
    """The raw resume_pending map — a NON-consuming read, so an assertion about the
    bridge store cannot itself spend the thing under test."""
    import json
    if not store.path.exists():
        return {}
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    return raw.get("resume_pending") or {}


def _tool(tmp_path, name="consolidate_records"):
    """A tool with NO effect tags (⇒ UNKNOWN) and a name carrying no verb the name
    map escalates on — so the verdict is driven purely by the params."""
    impl_dir = tmp_path.parent / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / f"{name}.py"
    impl.write_text(_BODY, encoding="utf-8")
    t = Tool(id=f"tool_{name}", name=name, description="d",
             tool_type=ToolType.PYTHON_FUNCTION,
             implementation_path=str(impl.relative_to(tmp_path.parent)),
             effect_tags=[], version=1)
    sig = tool_signature(name, hashlib.sha1(impl.read_bytes()).hexdigest(),
                         set(), host_class="")
    return t, sig


class _Posts:
    """Captures every card the gate posts."""
    def __init__(self):
        self.cards = []

    def inbox(self, outer):
        class _FakeInbox:
            def __init__(self, vault):
                pass

            def enqueue(_self, descriptor, *, gate_type="tool", context_extras=None, **kw):
                outer.cards.append({"descriptor": descriptor,
                                    "extras": dict(context_extras or {}),
                                    "gate_type": gate_type})
                return f"dec_{len(outer.cards)}"
        return _FakeInbox

    @property
    def last(self):
        return self.cards[-1]

    def __len__(self):
        return len(self.cards)


class _Vault:
    """Enough vault for the resolved-dedup bypass path."""
    def __init__(self, resolved_choice=None):
        self._resolved_choice = resolved_choice
        self.consumed = 0

    def load_index(self, name):
        return []


def _sandbox(tmp_path, posts, monkeypatch, vault=None):
    from systemu.runtime.tool_sandbox import ToolSandbox
    monkeypatch.setattr("systemu.interface.command.inbox.InboxQueue", posts.inbox(posts))
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    sb = ToolSandbox(str(tmp_path), vault=vault or _Vault(),
                     command_approvals=store)
    return sb, store


def _run(sb, tool, params, **kw):
    return asyncio.run(sb.execute_tool(tool.implementation_path, params,
                                       tool=tool, **kw))


# ── precondition: the tool really is in the DENY band on these params ────────

def test_precondition_the_call_is_deny_band(tmp_path, monkeypatch):
    from systemu.runtime.action_governance import (
        ActionContext, Verdict, evaluate_action)
    from systemu.runtime.tool_sandbox import ToolSandbox

    def _v(params, assigned=None):
        return evaluate_action(ActionContext(
            tool="consolidate_records", effect_tags=set(),
            operator_assigned_class=assigned,
            is_destructive_param=ToolSandbox.is_destructive_call(
                "consolidate_records", params)))[0]

    assert _v(NASTY) == Verdict.DENY
    # and the operator's class is exactly what lifts it to an approvable card
    assert _v(NASTY, "local_write") == Verdict.REQUIRE_APPROVAL


# ── Step 3: the gate reads the reclassification BEFORE scoring ───────────────

def test_a_pending_reclassification_lifts_the_card_off_the_deny_band(tmp_path, monkeypatch):
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert posts.last["extras"]["verdict"] == "deny"
    assert RECLASSIFY_OPTION in posts.last["descriptor"].options

    _reclassify(store, sig, "local_write", NASTY)

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    follow_up = posts.last
    assert follow_up["extras"]["verdict"] == "require_approval"   # the NEW verdict
    assert follow_up["extras"]["reclassified"] is True
    assert follow_up["extras"]["assigned_class"] == "local_write"
    assert follow_up["extras"]["requires_typed_confirm"] is True
    assert follow_up["descriptor"].options == ["Deny", "Approve once"]


def test_the_follow_up_card_does_not_consume_the_reclassification(tmp_path, monkeypatch):
    """The card is POSTED, not run — the classification must survive to be spent by
    the approved re-run. Consuming it here would make the operator's "Approve once"
    land on a call that DENYs again: an unresolvable loop."""
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert _peek(store, sig, NASTY) == "local_write"


def test_a_garbage_reclassification_in_the_store_does_not_lift_the_deny(tmp_path, monkeypatch):
    """Belt and braces against a hand-edited store: a class that classifies nothing
    must leave the call in the DENY band."""
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    import json
    path = tmp_path / "command_approvals.json"
    path.write_text(json.dumps({"version": 1, "reclassified": {
        sig: {"effect_class": "made_up", "recorded_at": "x"}}}), encoding="utf-8")

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert posts.last["extras"]["verdict"] == "deny"
    assert posts.last["extras"].get("reclassified") is not True


# ── SILENCE HOLE 1: a standing allow must not skip the follow-up card ───────

def test_a_standing_allow_does_not_silence_the_follow_up_card(tmp_path, monkeypatch):
    """The operator granted "Always allow" back when this tool scored
    REQUIRE_APPROVAL on benign params. The signature is params-INDEPENDENT, so that
    allow also matches this destructive call — which the gate has just DENY-floored
    and the operator has just reclassified. Honouring it would run the call with the
    operator having seen NO card on the new classification. The honest follow-up card
    must post at least once."""
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)

    store.approve(sig, command="consolidate_records")
    # the benign call still runs frictionlessly — the carve-out is surgical
    _run(sb, tool, BENIGN)
    assert len(posts) == 0

    _reclassify(store, sig, "local_write", NASTY)
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert len(posts) == 1
    assert posts.last["extras"]["reclassified"] is True
    # and the standing allow was NOT spent or revoked by the refusal
    assert store.is_approved(sig) is True


def test_a_standing_allow_still_works_with_no_reclassification_pending(tmp_path, monkeypatch):
    # the skip is conditional on a PENDING reclassification, nothing more
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    store.approve(sig, command="consolidate_records")
    _run(sb, tool, BENIGN)
    assert len(posts) == 0


# ── SILENCE HOLE 2: the reclassify decision is not itself an approval ───────

class _Decision:
    """A resolved decision the way the queue hands one back — choice + the context
    the gate scope-checks."""
    def __init__(self, choice, context=None):
        self.id, self.choice, self.context = "dec_x", choice, dict(context or {})


def _queue_returning(monkeypatch, decision, counter=None):
    """Stub the queue at the ONE method the gate now calls. Deliberately does NOT
    implement ``consume_resolved_choice``: a stub that still answered the old method
    would let a regression to it pass unnoticed."""
    class _Q:
        def __init__(self, vault):
            pass

        def consume_resolved_decision(self, dedup):
            if counter is not None:
                counter["n"] += 1
            return decision

    monkeypatch.setattr(
        "systemu.approval.decision_queue.OperatorDecisionQueue", _Q)


def _follow_up_ctx(cls, params):
    """The context the gate itself stamps on a FOLLOW-UP card (tool_sandbox
    ``_resume_extras``) — i.e. the only decision shape allowed to bypass while a
    reclassification is pending."""
    return {"reclassified": True, "assigned_class": cls,
            "args_fingerprint": args_fingerprint(params)}


def test_the_resolved_reclassify_decision_is_not_an_approval(tmp_path, monkeypatch):
    """The chat lane threads the resolved decision's dedup back in as a one-shot
    "Approve once" token. The reclassify resolution shares that dedup key — if the
    bypass reads it as "not Deny, therefore approved", the reclassify click itself
    runs the call and no follow-up card is ever posted.

    The decision handed back here is IN SCOPE in every other respect (it carries the
    follow-up card's own class + fingerprint), so the ONLY thing that can refuse it is
    the reclassify-label guard. Without that the test would pass on the strength of the
    scope check and stop covering the label.
    """
    posts = _Posts()
    consumed = {"n": 0}
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)
    _queue_returning(monkeypatch,
                     _Decision(RECLASSIFY_OPTION,
                               _follow_up_ctx("local_write", NASTY)),
                     consumed)

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY, _command_gate_resolved=f"tool:{sig}")
    assert consumed["n"] == 1, "the stale reclassify decision must still be retired"
    assert len(posts) == 1, "a follow-up card must post"
    assert posts.last["extras"]["reclassified"] is True


def test_a_resolved_approve_once_from_another_card_does_not_bypass(tmp_path, monkeypatch):
    """REWRITTEN (adversarial review round 3, CRITICAL). This test used to assert the
    OPPOSITE — that any resolved "Approve once" on the dedup key bypasses, commented
    "the ordinary one-shot is intact" — which baked in the vulnerability and directly
    contradicted ``test_the_bridge_is_the_only_bypass_while_a_reclassification_is_pending``
    two functions below.

    It is not an "ordinary one-shot". The dedup key ``tool:<sig>`` is
    params-INDEPENDENT and a resolved row is never retired by the approve→resume flow,
    so this choice is routinely a leftover from a DIFFERENT card — and honouring it
    cashes the operator's reclassification and runs the DENY-band call with no card
    ever approved for the assigned class. Full end-to-end reproduction through
    production code: ``tests/test_impl2_resolved_dedup_scope.py``.
    """
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)
    # an approval from an ordinary card: no classification scope at all
    _queue_returning(monkeypatch, _Decision("Approve once", {}))

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY, _command_gate_resolved=f"tool:{sig}")
    assert len(posts) == 1, "the follow-up card must post"
    assert posts.last["extras"]["reclassified"] is True
    assert _peek(store, sig, NASTY) == "local_write", (
        "the reclassification must survive — the call did not run")


def test_the_follow_up_cards_own_approve_once_does_bypass(tmp_path, monkeypatch):
    """…and the channel is SCOPED, not closed. This is the decision the chat lane
    carries when the operator approves the follow-up card inline, and it must still
    run the call exactly once."""
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)
    _queue_returning(monkeypatch,
                     _Decision("Approve once", _follow_up_ctx("local_write", NASTY)))

    _run(sb, tool, NASTY, _command_gate_resolved=f"tool:{sig}")
    assert len(posts) == 0
    # …and it spent the reclassification, because the call RAN
    assert _peek(store, sig, NASTY) is None


def test_an_ordinary_approve_once_bypasses_with_no_reclassification_pending(
        tmp_path, monkeypatch):
    """The scope check is conditional on a PENDING reclassification, nothing more —
    an unscoped decision on an ordinary REQUIRE_APPROVAL call is honoured as before."""
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _queue_returning(monkeypatch, _Decision("Approve once", {}))

    _run(sb, tool, BENIGN, _command_gate_resolved=f"tool:{sig}")
    assert len(posts) == 0


# ── SILENCE HOLE 3: the reclassification is spent by the call it authorised ──
#
# REWRITTEN (adversarial review). Both of these used to pre-seed the bridge with a
# bare ``store.mark_resume_approved(sig)`` — i.e. they asserted that ANY unconsumed
# bridge on the signature cashes the reclassification. That is precisely the
# vulnerability (see ``test_a_stale_deny_card_bridge_cannot_cash_the_reclassification``):
# the bridge is now SCOPED to the follow-up card that minted it, so the fixture has to
# mint it the way the follow-up card does or it is testing a bypass that no longer
# exists. The assertions are unchanged; only the provenance of the bridge is.


def _follow_up_bridge(store, sig, cls):
    """Mint the bridge the FOLLOW-UP card mints — i.e. what the dispatcher records
    when the operator clicks "Approve once" on a card posted under ``cls``."""
    from systemu.runtime import resume_on_decision as rod
    import systemu.runtime.command_approvals as ca
    _prev = ca.init_default_store
    ca.init_default_store = lambda _p: store
    try:
        rod._record_gate_approval(
            {"tool_signature": sig, "verdict": "require_approval",
             "reclassified": True, "assigned_class": cls},
            is_tool_gate=True, choice="approve once")
    finally:
        ca.init_default_store = _prev


def test_the_bridge_bypass_consumes_the_reclassification(tmp_path, monkeypatch):
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)
    _follow_up_bridge(store, sig, "local_write")

    _run(sb, tool, NASTY)                       # runs via the single-use bridge
    assert len(posts) == 0
    assert _peek(store, sig, NASTY) is None, "single-use — spent by the call it ran"


def test_only_the_follow_up_cards_own_bridge_bypasses_in_the_workflow_lane(tmp_path, monkeypatch):
    """RENAMED (round 3). The old name — "the bridge is the ONLY bypass" — was the
    belief that let the resolved-dedup channel ship unscoped: it reads as a proof that
    every other path is closed, when this test never drives the chat lane's channel at
    all (``resolved_dedup`` is None here). There are TWO bypasses under a pending
    reclassification, one per lane, and each is scope-bound to the follow-up card. This
    is the workflow-lane half; the chat-lane half is
    ``test_a_resolved_approve_once_from_another_card_does_not_bypass`` and its
    end-to-end twin in ``test_impl2_resolved_dedup_scope.py``.
    """
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)
    store.approve(sig, command="consolidate_records")     # standing: must not apply

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    # now add the follow-up card's own one-shot; THAT works
    _follow_up_bridge(store, sig, "local_write")
    _run(sb, tool, NASTY)
    assert len(posts) == 1


# ── THE ATTACK: a stale bridge cashing the reclassification ──────────────────

def test_a_stale_deny_card_bridge_cannot_cash_the_reclassification(tmp_path, monkeypatch):
    """THE REGRESSION PIN (adversarial review, CRITICAL).

    "Approve once" on a DENY card is a no-op AT THE GATE — every bypass sits under
    ``if verdict != Verdict.DENY``. But the RECORDER was not band-aware: it fell to
    its else-branch and minted a resume bridge anyway. Harmless while DENY skipped
    every bypass; the moment IMPL-2 lifts those same params to REQUIRE_APPROVAL, that
    stale bridge becomes consumable — and the bridge is the ONE bypass deliberately
    left live under a pending reclassification.

    Full path, no special capability: DENY card → operator clicks "Approve once" (the
    natural first move) → operator then uses the remedy → the re-run cashes the STEP-2
    bridge, spends the reclassification, and executes the destructive call with the
    operator having seen NO card on the classification they assigned.
    """
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    import systemu.runtime.command_approvals as ca
    from systemu.runtime import resume_on_decision as rod
    monkeypatch.setattr(ca, "init_default_store", lambda _p: store)

    # 1. the destructive call DENYs and posts its card
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    deny_extras = dict(posts.last["extras"])
    assert deny_extras["verdict"] == "deny"

    # 2. the operator clicks "Approve once" on it. Whatever surface supplies that
    #    choice, the recorder must mint NOTHING for a DENY-band gate.
    rod._record_gate_approval(deny_extras, is_tool_gate=True, choice="approve once")
    assert _bridges(store) == {}, (
        "a DENY-band gate must record NO approval of any kind — a bridge minted here "
        "is redeemable the instant a reclassification lifts the verdict")

    # 3. it still DENYs on a re-run (the gate half of the no-op contract)
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert posts.last["extras"]["verdict"] == "deny"

    # 4. the operator uses the actual remedy
    rod._record_reclassification({"tool_signature": sig,
                                  "assigned_class": "local_write",
                                  "typed_confirmed": True,
                                  "args_fingerprint": args_fingerprint(NASTY)})
    assert _peek(store, sig, NASTY) == "local_write"

    # 5. the re-run must POST the follow-up card, not run the call
    n_before = len(posts)
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert len(posts) == n_before + 1, "the follow-up card must be posted"
    assert posts.last["extras"]["reclassified"] is True
    assert _peek(store, sig, NASTY) == "local_write", (
        "the reclassification must survive to be spent by the APPROVED re-run")


def test_a_bridge_left_by_an_unrelated_run_cannot_cash_a_reclassification(tmp_path, monkeypatch):
    """FIX 2, pinned independently of the DENY-band recorder rule.

    A legitimate REQUIRE_APPROVAL "Approve once" whose run crashed before re-entry
    leaves an unconsumed bridge. It was minted for a benign call and carries no
    reclassification scope, so a later reclassification must not be redeemable by it.
    Recording the assignment CLEARS any bridge standing on that signature.
    """
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    import systemu.runtime.command_approvals as ca
    from systemu.runtime import resume_on_decision as rod
    monkeypatch.setattr(ca, "init_default_store", lambda _p: store)

    store.mark_resume_approved(sig)               # the orphaned benign one-shot
    assert _bridges(store), "fixture precondition"

    rod._record_reclassification({"tool_signature": sig,
                                  "assigned_class": "local_write",
                                  "typed_confirmed": True,
                                  "args_fingerprint": args_fingerprint(NASTY)})
    assert _bridges(store) == {}, "recording a reclassification clears the bridge store"

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert posts.last["extras"]["reclassified"] is True


def test_a_bridge_scoped_to_another_class_does_not_bypass(tmp_path, monkeypatch):
    """FIX 3, pinned independently of fixes 1 and 2.

    The bridge carries the classification of the card that minted it. A bridge minted
    on a ``net_mutate`` follow-up card is not the operator's decision about a
    ``local_write`` one, so it must not satisfy the gate — the record is left standing
    (it is still a legitimate one-shot for its own card) and a fresh card posts.
    """
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)
    _follow_up_bridge(store, sig, "net_mutate")           # the WRONG card's one-shot

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert posts.last["extras"]["reclassified"] is True
    assert _peek(store, sig, NASTY) == "local_write", "not spent by a refused bypass"


def test_an_unscoped_bridge_does_not_bypass_under_a_pending_reclassification(tmp_path, monkeypatch):
    """The symmetric half of FIX 3: an ordinary (unscoped) bridge is not a decision
    about a reclassified call either. Pinned separately because the class-mismatch
    test above would still pass if the guard only compared two non-empty scopes."""
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)
    store.mark_resume_approved(sig)                       # no scope at all

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert posts.last["extras"]["reclassified"] is True


def test_a_scoped_bridge_does_not_bypass_an_ordinary_call(tmp_path, monkeypatch):
    """…and the other direction. A bridge minted under a one-shot classification must
    not survive to cover an ordinary REQUIRE_APPROVAL call on the same signature once
    the reclassification is gone — it was granted for a call scored under a class the
    operator assigned, not for this one."""
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _follow_up_bridge(store, sig, "local_write")          # scoped, no record pending

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert posts.last["extras"]["verdict"] == "deny"


# ── THE SUBSTITUTION: the record is params-scoped ────────────────────────────

def test_a_reclassification_does_not_transfer_to_different_parameters(tmp_path, monkeypatch):
    """HIGH (adversarial review). The record is keyed on the params-INDEPENDENT tool
    signature, but the DENY verdict is params-DEPENDENT. The operator reclassifies
    after a DENY on ``/data/quarterly_report``; the agent then issues the same tool
    against ``/etc/production_secrets``. Both calls share a signature, so without a
    params fingerprint call B is lifted — and ``from_tool`` never receives parameters,
    so the card it posts is byte-identical to the one expected for call A. Nothing on
    the surface would let the operator notice the swap.
    """
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)

    # the SUBSTITUTED call must not inherit the assignment
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, OTHER_NASTY)
    assert posts.last["extras"]["verdict"] == "deny", (
        "a different call must fall back to a fresh DENY")
    assert posts.last["extras"].get("reclassified") is not True
    # …and it did not spend the record the operator made for the OTHER call
    assert _peek(store, sig, NASTY) == "local_write"

    # the call it was actually assigned to still lifts
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert posts.last["extras"]["reclassified"] is True


def test_the_reclassified_card_discloses_which_call_it_is(tmp_path, monkeypatch):
    """The operator must be able to see WHICH call they are approving. Without the
    args in the card, two different calls on one signature render identically."""
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    _reclassify(store, sig, "local_write", NASTY)

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    inspect = posts.last["descriptor"].inspect
    assert "/data/quarterly_report" in inspect, inspect
    assert "--force" in inspect, inspect


def test_the_fingerprint_describes_the_call_that_will_actually_run(tmp_path, monkeypatch):
    """The sandbox REWRITES some parameters (the output-dir path redirect) before the
    gate scores them. The fingerprint must therefore describe the post-rewrite call —
    the one that will actually execute — and must be stable across gate entries, or
    the remedy would never be redeemable for any tool taking a path.
    """
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)
    redirected = {"path": "/data/quarterly_report", "flags": "--force"}

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, redirected)
    fp_one = posts.last["extras"]["args_fingerprint"]
    assert fp_one, "the gate must stamp a fingerprint on every tool card"
    # the raw dict is NOT what was gated — the path was rewritten first
    assert fp_one != args_fingerprint(redirected)

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, dict(redirected))
    assert posts.last["extras"]["args_fingerprint"] == fp_one, "stable across entries"

    # …and a record made against the stamped fingerprint really does lift the call
    store.mark_reclassified(sig, "local_write", args_fingerprint=fp_one)
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, dict(redirected))
    assert posts.last["extras"]["reclassified"] is True

    # a DIFFERENT path is a different call even after redirection
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, {"path": "/etc/production_secrets", "flags": "--force"})
    assert posts.last["extras"]["verdict"] == "deny"


# ── THE END-TO-END TRACE ─────────────────────────────────────────────────────

def test_the_end_to_end_reclassify_trace(tmp_path, monkeypatch):
    posts = _Posts()
    sb, store = _sandbox(tmp_path, posts, monkeypatch)
    tool, sig = _tool(tmp_path)

    # 1. the DENY card
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert len(posts) == 1
    deny_card = posts.last["descriptor"]
    assert posts.last["extras"]["verdict"] == "deny"
    assert deny_card.options == ["Deny", RECLASSIFY_OPTION]
    assert "Always allow" not in deny_card.options
    assert "Approve once" not in deny_card.options, (
        "a DENY-band approval is a no-op at the gate; offering it is what minted the "
        "stale bridge the reclassification lift could cash")

    # 2. the operator reclassifies (what the dispatcher does on that choice), under
    #    the TYPED confirmation the Inbox panel stamps.
    from systemu.runtime import resume_on_decision as rod
    import systemu.runtime.command_approvals as ca
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)
    rod._record_reclassification({"tool_signature": sig,
                                  "assigned_class": "local_write",
                                  "typed_confirmed": True,
                                  "args_fingerprint": args_fingerprint(NASTY)})
    assert _peek(store, sig, NASTY) == "local_write"

    # 3. the resumed run posts a REQUIRE_APPROVAL follow-up — and runs NOTHING
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert len(posts) == 2
    follow_up = posts.last
    assert follow_up["extras"]["verdict"] == "require_approval"
    assert follow_up["extras"]["reclassified"] is True
    assert follow_up["descriptor"].options == ["Deny", "Approve once"]
    assert follow_up["descriptor"].risk == "high"

    # 4. "Approve once" on the follow-up → a single-use bridge (never standing)
    rod._record_gate_approval(follow_up["extras"], is_tool_gate=True,
                              choice="approve once")
    assert store.is_approved(sig) is False

    # 5. the resumed run executes EXACTLY ONCE and spends the reclassification
    _run(sb, tool, NASTY)
    assert len(posts) == 2, "no card — the bridge covered this call"
    assert _peek(store, sig, NASTY) is None

    # 6. the very next identical call is back in the DENY band
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    assert len(posts) == 3
    assert posts.last["extras"]["verdict"] == "deny"
    assert posts.last["descriptor"].options == ["Deny", RECLASSIFY_OPTION]


# ── Step 4: the card shapes ──────────────────────────────────────────────────

def test_a_deny_card_offers_the_reclassify_remedy():
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny")
    assert d.options == ["Deny", RECLASSIFY_OPTION]
    assert d.safe_default == "Deny" and d.options[0] == "Deny"
    assert "Always allow" not in d.options
    assert d.risk == "high"


def test_a_deny_card_does_not_offer_approve_once():
    """It does nothing at the gate by contract (every bypass sits under
    ``verdict != DENY``), and offering it is what minted the stale resume bridge that
    a later reclassification lift could cash. The remedy is the reclassify option."""
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny")
    assert "Approve once" not in d.options
    # the FOLLOW-UP card is where an approval genuinely belongs
    f = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval",
                                 reclassified=True, assigned_class="local_write")
    assert f.options == ["Deny", "Approve once"]


def test_a_deny_card_explains_that_reclassify_runs_nothing():
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny")
    text = d.what_approve_does.lower()
    assert "cannot be remembered" in text          # the existing promise is kept
    assert "reclassif" in text
    assert "never runs" in text or "does not run" in text


def test_a_require_approval_card_is_unchanged():
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval")
    assert d.options == ["Deny", "Approve once", "Always allow"]
    assert RECLASSIFY_OPTION not in d.options
    assert d.safe_default == "Deny" and d.risk == "medium"


def test_the_follow_up_card_offers_no_standing_allow_and_no_second_reclassify():
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval",
                                 reclassified=True, assigned_class="local_write")
    assert d.options == ["Deny", "Approve once"]
    assert "Always allow" not in d.options
    assert RECLASSIFY_OPTION not in d.options
    assert d.safe_default == "Deny"
    assert d.risk == "high", "a once-refused effect keeps its high-risk treatment"


def test_the_follow_up_card_names_the_assigned_class():
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval",
                                 reclassified=True, assigned_class="local_write")
    assert "operator-reclassified as local_write" in d.inspect


def test_a_reclassified_deny_card_still_offers_no_standing_allow():
    # a reclassified card that somehow still scores DENY keeps the tight option set
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny",
                                 reclassified=True, assigned_class="local_write")
    assert d.options == ["Deny", "Approve once"]
    assert d.risk == "high"


def test_the_reclassify_option_label_is_the_shared_constant():
    # decision_queue.resolve validates choice-in-options, so the card label and the
    # UI's resolve choice must BYTE-match. One constant, imported by both.
    assert RECLASSIFY_OPTION == "Reclassify effect…"
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny")
    assert d.options[-1] == RECLASSIFY_OPTION


# ── Step 4: a once-DENY-floored effect never becomes remotely tap-approvable ──

def _ctx(descriptor, extras):
    """Exactly how InboxQueue.enqueue merges the two (extras first, descriptor wins)."""
    return {**dict(extras), **descriptor.to_decision_context(gate_type="tool")}


def test_the_follow_up_card_floors_remote_resolution():
    from systemu.messaging.decision_bridge import classify_resolution
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval",
                                 reclassified=True, assigned_class="local_write")
    ctx = _ctx(d, {"tool_signature": "s", "verdict": "require_approval",
                   "effect_tags": [], "destructive": True,
                   "reclassified": True, "assigned_class": "local_write",
                   "requires_typed_confirm": True})
    assert classify_resolution(ctx) == "floor"


def test_the_typed_confirm_key_is_what_floors_it():
    """Pin the MECHANISM, not just the outcome: strip the typed-confirm marker and
    the same context becomes remote. Without this the test could pass because of the
    destructive flag alone and silently stop covering the marker.

    ``effect_tags`` must carry a REAL positive classification for that isolation to
    work. It used to be ``[]``, which was incidental to this test but is now itself a
    floor trigger (an empty list is the ABSENCE of a classification, not "no effect"
    — see classify_resolution step 3), which would have made the second assertion
    floor for the wrong reason and silently stopped covering the marker again.
    ``local_write`` is also what the real producer now stamps here: the operator
    assigned that class, so it is in the tag set ``evaluate_action`` scored."""
    from systemu.messaging.decision_bridge import classify_resolution
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval",
                                 reclassified=True, assigned_class="local_write")
    base = _ctx(d, {"tool_signature": "s", "verdict": "require_approval",
                    "effect_tags": ["local_write"], "destructive": False,
                    "reclassified": True, "assigned_class": "local_write"})
    assert classify_resolution({**base, "requires_typed_confirm": True}) == "floor"
    assert classify_resolution(base) == "remotely_resolvable"


def test_a_deny_card_floors_remote_resolution():
    from systemu.messaging.decision_bridge import classify_resolution
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny")
    ctx = _ctx(d, {"tool_signature": "s", "verdict": "deny", "effect_tags": [],
                   "destructive": True})
    assert classify_resolution(ctx) == "floor"


# ── Step 5: the Inbox reclassify panel (pure helpers) ───────────────────────

def test_reclassify_choices_are_the_real_effect_tags_minus_unknown():
    from systemu.interface.pages.inbox_page import _reclassify_choices
    from systemu.runtime.effect_tags import EffectTag
    choices = _reclassify_choices()
    assert choices, "the operator must have something to assign"
    assert EffectTag.UNKNOWN.value not in choices
    assert set(choices) == {t.value for t in EffectTag if t is not EffectTag.UNKNOWN}
    # they are the REAL values the store + governor accept, not display labels
    assert "money_move" in choices and "local_write" in choices


def test_reclassify_validation_requires_an_exact_typed_match():
    from systemu.interface.pages.inbox_page import _validate_reclassify
    ok, err = _validate_reclassify("local_write", "local_write")
    assert ok == "local_write" and err == ""

    for selected, typed in [("local_write", ""),
                            ("local_write", "local_writ"),
                            ("local_write", "LOCAL_WRITE"),
                            ("local_write", " local_write "),
                            ("local_write", "net_mutate"),
                            ("", "local_write"),
                            ("", "")]:
        ok, err = _validate_reclassify(selected, typed)
        assert ok is None, f"{selected!r}/{typed!r} must not confirm"
        assert err, "a refusal must tell the operator why"


def test_reclassify_validation_refuses_a_class_outside_the_vocabulary():
    from systemu.interface.pages.inbox_page import _validate_reclassify
    ok, err = _validate_reclassify("made_up", "made_up")
    assert ok is None and err
    ok, err = _validate_reclassify("unknown", "unknown")
    assert ok is None and err, "'unknown' is the conjunct the DENY band keys on"


def test_the_inbox_card_offers_the_reclassify_panel_only_for_tool_gates():
    from systemu.interface.pages.inbox_page import _wants_reclassify_panel
    # the REAL DENY card shape (no "Approve once" — it does nothing at the gate)
    assert _wants_reclassify_panel("tool:abc", ["Deny", RECLASSIFY_OPTION])
    assert not _wants_reclassify_panel("tool:abc", ["Deny", "Approve once"])
    assert not _wants_reclassify_panel("command:abc", ["Deny", RECLASSIFY_OPTION])
    assert not _wants_reclassify_panel("", [])


# ── the chat lane reports a reclassify honestly ─────────────────────────────

def test_chat_lane_does_not_report_a_reclassify_as_a_timeout(monkeypatch):
    """A reclassify is neither approval nor denial. It must not be reported with the
    timeout copy — nothing timed out, and the operator DID act. The call still does not
    run (a fresh card is posted on the new classification), so this stays fail-closed."""
    from systemu.pipelines import quick_task
    from systemu.approval.exceptions import PendingOperatorDecision

    monkeypatch.setattr(quick_task, "_poll_command_choice",
                        lambda vault, dedup: "Reclassify effect…", raising=False)

    from systemu.core.models import Tool, ToolType

    class _SB:
        _vault = None
        async def execute_tool(self, *a, **k):
            raise PendingOperatorDecision("dec_1", "tool:sig-1",
                                         ["Deny", "Approve once", "Reclassify effect…"])

    tool = Tool(id="t1", name="run_command", description="d",
                tool_type=ToolType.PYTHON_FUNCTION, implementation_path="x.py", version=1)
    out = quick_task._execute_tool(_SB(), tool, {"command": "rm -rf /"})
    err = (getattr(out, "error", "") or "").lower()
    assert "timed out" not in err, err          # the untrue message
    assert "reclassif" in err                    # says what actually happened
    assert getattr(out, "success", True) is False   # still fail-closed
    # …and it must not promise a card. The attempt is ABANDONED here; whether a
    # follow-up card appears depends on the ReAct loop retrying, which this lane does
    # not control. "Approve the follow-up card" was a promise the code cannot keep.
    assert "follow-up card" not in err, err
    assert "approve the" not in err, err


# ── MED: the typed confirmation is enforced in the RECORDER, not just the UI ──

def test_a_reclassification_without_typed_confirmation_records_nothing(tmp_path, monkeypatch):
    """``resolve_with_context_patch`` is a public API with no notion of caller, and
    nothing outside the Inbox panel had ever READ ``typed_confirmed``. The gesture the
    remedy is predicated on must be checked where the record is made."""
    from systemu.runtime import resume_on_decision as rod
    import systemu.runtime.command_approvals as ca
    store = CommandApprovalStore(tmp_path / "ca.json")
    monkeypatch.setattr(ca, "init_default_store", lambda _p: store)
    base = {"tool_signature": "sig-1", "assigned_class": "local_write",
            "args_fingerprint": args_fingerprint(NASTY)}

    for missing in ({}, {"typed_confirmed": False}, {"typed_confirmed": ""},
                    {"typed_confirmed": None}):
        assert rod._record_reclassification({**base, **missing}) is None
        assert store.peek_reclassified(
            "sig-1", args_fingerprint=args_fingerprint(NASTY)) is None

    assert rod._record_reclassification({**base, "typed_confirmed": True}) == "local_write"


def test_a_reclassification_without_an_args_fingerprint_records_nothing(tmp_path, monkeypatch):
    """A record that cannot be matched to a call would apply to ANY call on the
    signature — exactly the substitution hole. Fail closed rather than record it.

    HONEST NOTE on what this pins. The outcome is OVER-DETERMINED: ``mark_reclassified``
    refuses an empty fingerprint on its own, so this test passes with the recorder's
    own ``if not fingerprint`` guard removed (mutation-checked — the whole IMPL-2 suite
    stays green without it). It pins the OUTCOME, which is the thing that must never
    regress; the store-layer refusal is separately pinned by
    ``test_marking_without_a_fingerprint_records_nothing``, which DOES fail when its
    guard is removed. The recorder-side check is kept as defence in depth and to keep
    the log message accurate about which precondition failed.
    """
    from systemu.runtime import resume_on_decision as rod
    import systemu.runtime.command_approvals as ca
    store = CommandApprovalStore(tmp_path / "ca.json")
    monkeypatch.setattr(ca, "init_default_store", lambda _p: store)
    assert rod._record_reclassification({"tool_signature": "sig-1",
                                         "assigned_class": "local_write",
                                         "typed_confirmed": True}) is None


# ── MED: surfaces that cannot supply the panel must not offer the remedy ─────

def test_the_generic_decision_card_suppresses_the_reclassify_option():
    """``insights.render_decision_card`` renders every option as a plain button that
    calls ``queue.resolve(id, choice=label)``. Clicking "Reclassify effect…" there
    resolves the card, records NOTHING (no context patch, no assigned class) and
    re-DENYs — the operator sees the remedy, uses it, and lands back in the dead end
    this packet exists to remove, on a surface that looks identical to the Inbox."""
    from systemu.interface.pages.insights import _renderable_options
    kept, suppressed = _renderable_options(["Deny", RECLASSIFY_OPTION])
    assert kept == ["Deny"]
    assert suppressed is True

    kept, suppressed = _renderable_options(["Deny", "Approve once", "Always allow"])
    assert kept == ["Deny", "Approve once", "Always allow"]
    assert suppressed is False

    # never renders an empty button row
    kept, suppressed = _renderable_options([RECLASSIFY_OPTION])
    assert kept == ["Deny"] and suppressed is True


def test_the_cli_refuses_to_resolve_a_reclassify():
    """Same dead end via ``sharing_on decisions resolve``: no panel, no context patch.
    Refuse and point at the Inbox rather than burn the card for nothing."""
    from systemu.interface.cli_commands import _reclassify_needs_the_inbox
    assert _reclassify_needs_the_inbox(RECLASSIFY_OPTION) is True
    assert _reclassify_needs_the_inbox("reclassify effect") is True
    assert _reclassify_needs_the_inbox("Deny") is False
    assert _reclassify_needs_the_inbox("Approve once") is False


def test_cli_resolve_rejects_the_reclassify_choice_without_resolving(tmp_path):
    from click.testing import CliRunner
    from systemu.interface import cli_commands

    resolved = []

    class _Q:
        def __init__(self, vault):
            pass

        def resolve(self, did, *, choice):
            resolved.append((did, choice))
            raise AssertionError("must not reach resolve")

    import systemu.approval.decision_queue as dq
    runner = CliRunner()
    orig_q, orig_v = dq.OperatorDecisionQueue, cli_commands._get_vault_and_config
    dq.OperatorDecisionQueue = _Q
    cli_commands._get_vault_and_config = lambda ctx: (None, object())
    try:
        res = runner.invoke(cli_commands.decisions_group,
                            ["resolve", "dec_1", "--choice", RECLASSIFY_OPTION])
    finally:
        dq.OperatorDecisionQueue, cli_commands._get_vault_and_config = orig_q, orig_v
    assert resolved == [], "the decision must not be resolved (and burned) here"
    assert res.exit_code != 0
    assert "inbox" in res.output.lower()


# ── LOW: a DENY card renders no affirmative button ───────────────────────────

def test_a_deny_card_model_has_no_affirmative_option():
    """``affirmative = options[-1]`` now lands on the reclassify label, which the
    renderer draws as a ghost — so the card would render with no primary/danger button
    at all AND the ghost styling would be decided in two places. A DENY card genuinely
    HAS no affirmative action; say so."""
    from systemu.interface.pages.inbox_page import _inbox_card_model
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="deny")
    m = _inbox_card_model(d)
    assert m["options"] == ["Deny", RECLASSIFY_OPTION]
    assert m["affirmative"] == "", "a refusal has no affirmative"
    assert m["safe_default"] == "Deny"


def test_the_follow_up_card_model_still_has_an_affirmative():
    from systemu.interface.pages.inbox_page import _inbox_card_model
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval",
                                 reclassified=True, assigned_class="local_write")
    assert _inbox_card_model(d)["affirmative"] == "Approve once"


def test_ordinary_card_models_keep_their_affirmative():
    """The carve-out must not leak into any card that has nothing to do with IMPL-2."""
    from systemu.interface.pages.inbox_page import _inbox_card_model
    d = GateDescriptor.from_tool(tool_name="t", sig="s", verdict="require_approval")
    assert _inbox_card_model(d)["affirmative"] == "Always allow"
    d2 = GateDescriptor(title="t", risk="high", options=["Dismiss", "Approve & Install"],
                        safe_default="Dismiss", what_approve_does="x", dedup="dep:r")
    assert _inbox_card_model(d2)["affirmative"] == "Approve & Install"
