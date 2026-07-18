"""IMPL-2 — the resolved-dedup channel is SCOPED to the follow-up card.

THE HOLE THIS FILE CLOSES (adversarial review round 3, CRITICAL).

``_maybe_gate_tool`` has two bypasses that survive under a pending operator
reclassification. Round 2 scope-stamped the first (the ``resume_pending`` bridge —
``for_reclassification``); the sibling in the same block was left unscoped:

    choice = OperatorDecisionQueue(vault).consume_resolved_choice(f"tool:{sig}")
    if choice is not None and norm != "deny" and not norm.startswith("reclassify"):
        return                      # ← ANY non-deny choice, from ANY card

The dedup key ``tool:<sig>`` is params-INDEPENDENT, so EVERY gate card this tool body
ever posted — benign or DENY-band — shares it. And a resolved decision row is never
retired by the ordinary flow: the resume dispatcher mints a bridge, the run resumes
through the BRIDGE, and the row stays ``status="resolved"`` in the queue forever.
``quick_task._poll_command_choice`` reads it with the NON-consuming
``get_resolved_choice``, so the poll returns instantly and the lane auto-retries with
``resolved_dedup`` set — the operator never touches the new card.

Net effect, with no special capability and no unusual sequence: a stale "Approve once"
from an ordinary benign call cashes the operator's reclassification and executes the
DENY-band call, while the follow-up card sits pending and unapproved.

THE INVARIANT: while a reclassification is pending, the only decision that may bypass
the gate is the FOLLOW-UP CARD's own resolution — proven by its context carrying
``reclassified is True``, ``assigned_class == pending_class``, and an
``args_fingerprint`` equal to this call's. Anything else re-gates.

The legitimate half is pinned just as hard
(``test_the_follow_up_cards_own_approval_still_bypasses_through_this_channel``):
this channel is what carries the chat lane's inline approve of the follow-up card, so
"skip the channel while a reclassification is pending" would have been a fix that
silently broke the remedy it exists to serve.
"""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from systemu.approval.decision_queue import OperatorDecision, OperatorDecisionQueue
from systemu.approval.exceptions import PendingOperatorDecision
from systemu.core.models import Tool, ToolType
from systemu.interface.command.gate import RECLASSIFY_OPTION
from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature
from systemu.runtime.tool_sandbox import args_fingerprint

# Same param sets the sibling gate suite uses: destructive (the params-dependent half
# of the DENY band) vs benign, with keys that the output-dir redirect does NOT rewrite.
NASTY = {"target": "/data/quarterly_report", "flags": "--force"}
BENIGN = {"target": "/data/quarterly_report"}
OTHER_NASTY = {"target": "/etc/production_secrets", "flags": "--force"}


# ── a vault with exactly the three methods OperatorDecisionQueue uses ─────────

class _MemVault:
    """The REAL queue, on an in-memory vault. Not a queue mock: every decision is
    posted, resolved and consumed through production ``OperatorDecisionQueue`` code,
    because the bug lives in how the gate READS those rows."""

    def __init__(self, root=None):
        self.root = root
        self._decisions = {}

    def load_index(self, name):
        if name != "decisions":
            return []
        return [d.to_dict() for d in self._decisions.values()]

    def get_decision(self, did):
        return self._decisions[did]

    def save_decision(self, decision):
        # Round-trip through the persisted shape so a test can never rely on an
        # in-memory object identity a real vault would not preserve.
        self._decisions[decision.id] = OperatorDecision.from_dict(decision.to_dict())


class _Sup:
    def __init__(self):
        self.submits = []

    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append((activity_id, shadow_id, kw.get("resume_from_execution_id")))


def _tool(tmp_path, sentinel):
    """A tool with NO effect tags (⇒ UNKNOWN) whose body WRITES A SENTINEL when it
    runs. "Did the destructive body execute?" is then an observation, not an
    inference from a return value.

    ISOLATION (learned the hard way — this suite was 1-in-3 flaky before it):
    the implementation lives under THIS test's ``tmp_path``, never a shared parent.
    Each test bakes a DIFFERENT absolute sentinel path into the body, and pytest
    truncates every tmp dir name to the same width — so a shared impl file gets
    successive writes of IDENTICAL SIZE, and CPython's ``__pycache__`` validation
    (size + mtime-to-the-second) happily reuses the PREVIOUS test's bytecode. The
    tool then "runs successfully" while writing another test's sentinel: a
    must-not-run assertion would pass for entirely the wrong reason.
    """
    body = (
        "from pathlib import Path\n"
        "def run(target=None, flags=None):\n"
        f"    Path(r'{sentinel}').write_text('RAN', encoding='utf-8')\n"
        "    return {'success': True}\n"
    )
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / "consolidate_records.py"
    impl.write_text(body, encoding="utf-8")
    t = Tool(id="tool_consolidate", name="consolidate_records", description="d",
             tool_type=ToolType.PYTHON_FUNCTION,
             implementation_path=str(impl.relative_to(tmp_path)),
             effect_tags=[], version=1)
    sig = tool_signature("consolidate_records",
                         hashlib.sha1(impl.read_bytes()).hexdigest(),
                         set(), host_class="")
    return t, sig


def _sandbox(tmp_path, vault):
    """The sandbox root is a SUBDIR of tmp_path, so ``vault_root.parent`` — the base the
    sandbox resolves a relative implementation_path against — is this test's own
    tmp_path rather than the session-wide pytest dir. See ``_tool``."""
    from systemu.runtime.tool_sandbox import ToolSandbox
    root = tmp_path / "vault"
    root.mkdir(parents=True, exist_ok=True)
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    return ToolSandbox(str(root), vault=vault, command_approvals=store), store


def _run(sb, tool, params, **kw):
    return asyncio.run(sb.execute_tool(tool.implementation_path, params,
                                       tool=tool, **kw))


def _pending(vault):
    return OperatorDecisionQueue(vault).list_pending()


def _resolved_rows(vault, dedup):
    return [d for d in vault._decisions.values()
            if d.dedup_key == dedup and d.status == "resolved"]


def _stamp_run_coords(monkeypatch):
    """Make the gate stamp resume coords, so the REAL ``_dispatch_resume`` can process
    the cards this test posts instead of taking its coords-less rescue branch."""
    import systemu.runtime.chat_submission_ctx as csc
    monkeypatch.setattr(csc, "current_execution_id", lambda: "exec_A", raising=False)
    monkeypatch.setattr(csc, "current_chat_submission_id", lambda: "sub_1", raising=False)
    monkeypatch.setattr(csc, "current_activity_id", lambda: "act_1", raising=False)
    monkeypatch.setattr(csc, "current_shadow_id", lambda: "shadow_1", raising=False)


def _bind_store(monkeypatch, store):
    """Point the resume dispatcher's default store at the sandbox's real store."""
    import systemu.runtime.command_approvals as ca
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    monkeypatch.setattr(ca, "init_default_store", lambda _p: store)


def _peek(store, sig, params):
    return store.peek_reclassified(sig, args_fingerprint=args_fingerprint(params))


# ═══ THE END-TO-END REPRODUCTION ═════════════════════════════════════════════

def test_a_stale_resolved_row_cannot_cash_a_reclassification_end_to_end(
        tmp_path, monkeypatch):
    """THE REGRESSION PIN. Production code end to end: real gate, real
    ``OperatorDecisionQueue``, real ``_dispatch_resume``, real ``quick_task`` chat lane,
    real subprocess execution.

    Every step is ordinary usage — no crash, no hand-edited store, no unusual click:

      1. the destructive call DENYs and posts its card;
      2. the operator uses the remedy (typed-confirm reclassify) → the assignment is
         recorded and the run is re-dispatched;
      3. meanwhile the agent makes a BENIGN call on the same tool. Different params, so
         the reclassification does not apply to it: an ordinary REQUIRE_APPROVAL card;
      4. the operator clicks "Approve once" on THAT card. The dispatcher mints the
         bridge, the resumed run consumes it and runs — and the decision ROW is left
         ``resolved`` forever, because nothing on the bridge path ever consumes it;
      5. the agent retries the DESTRUCTIVE call through the chat lane. The gate posts
         the follow-up card; ``_poll_command_choice`` (non-consuming) finds the stale
         row from step 4 — it is the NEWEST resolved row on this params-independent
         dedup key — and the lane auto-retries with ``resolved_dedup`` set.

    Before the fix, step 5 ran the destructive body with the DENY-band params while the
    follow-up card was still pending, and spent the operator's reclassification.
    """
    sentinel = tmp_path / "DESTRUCTIVE_BODY_RAN"
    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    tool, sig = _tool(tmp_path, sentinel)
    dedup = f"tool:{sig}"
    _stamp_run_coords(monkeypatch)
    _bind_store(monkeypatch, store)
    from systemu.runtime import resume_on_decision as rod
    queue = OperatorDecisionQueue(vault)

    # 1. the destructive call DENYs
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    deny_card = _pending(vault)[0]
    assert deny_card.context["verdict"] == "deny"

    # 2. the operator reclassifies, under the typed confirmation the panel stamps
    queue.resolve_with_context_patch(
        deny_card.id, choice=RECLASSIFY_OPTION,
        context_patch={"assigned_class": "local_write", "typed_confirmed": True})
    assert rod._dispatch_resume(vault.get_decision(deny_card.id),
                                vault=vault, supervisor=_Sup(),
                                data_dir=str(tmp_path)) is True
    assert _peek(store, sig, NASTY) == "local_write"

    # 3. an ORDINARY benign call on the same tool. The reclassification is params-scoped
    #    so it does not apply here — this is a plain REQUIRE_APPROVAL card.
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, BENIGN)
    benign_card = [d for d in _pending(vault) if d.id != deny_card.id][0]
    assert benign_card.context["verdict"] == "require_approval"
    assert benign_card.context.get("reclassified") is not True

    # 4. "Approve once" on it → the dispatcher mints the bridge, the resumed run
    #    consumes it and runs. NOTHING consumes the decision row.
    queue.resolve(benign_card.id, choice="Approve once")
    assert rod._dispatch_resume(vault.get_decision(benign_card.id),
                                vault=vault, supervisor=_Sup(),
                                data_dir=str(tmp_path)) is True
    _run(sb, tool, BENIGN)                     # resumes through the bridge
    sentinel.unlink(missing_ok=True)           # that benign run is not what we measure
    assert _resolved_rows(vault, dedup), (
        "fixture precondition: an approved run leaves its decision row resolved forever")

    # 5. the destructive retry, through the REAL chat lane
    from systemu.pipelines import quick_task
    out = quick_task._execute_tool(sb, tool, dict(NASTY))

    # ── the invariant ────────────────────────────────────────────────────────
    assert not sentinel.exists(), (
        "the DENY-band call executed on a stale approval from an unrelated card, with "
        "the follow-up card still pending and unapproved")
    assert getattr(out, "success", True) is False
    assert _peek(store, sig, NASTY) == "local_write", (
        "the operator's reclassification must survive — it was never approved")
    follow_ups = [d for d in _pending(vault)
                  if d.context.get("reclassified") is True]
    assert len(follow_ups) == 1, "the follow-up card must be posted and left pending"
    assert follow_ups[0].context["assigned_class"] == "local_write"


def test_the_follow_up_cards_own_approval_still_bypasses_through_this_channel(
        tmp_path, monkeypatch):
    """THE OTHER HALF, and the reason the fix SCOPES this channel rather than skipping
    it while a reclassification is pending.

    In the chat lane the operator's inline "Approve once" on the FOLLOW-UP card is
    carried by exactly this path: ``_poll_command_choice`` sees the resolution and the
    lane re-calls with ``resolved_dedup`` set. The scoped ``resume_pending`` bridge is
    minted by a DIFFERENT actor (the resume dispatcher) and need not exist — a lane
    with no resumable run never mints one at all. Skip this channel under a pending
    reclassification and the remedy becomes unusable: the operator approves the card
    and the call re-gates forever.
    """
    sentinel = tmp_path / "DESTRUCTIVE_BODY_RAN"
    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    tool, sig = _tool(tmp_path, sentinel)
    dedup = f"tool:{sig}"
    store.mark_reclassified(sig, "local_write",
                            args_fingerprint=args_fingerprint(NASTY))

    # the follow-up card posts…
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY)
    card = _pending(vault)[0]
    assert card.context["reclassified"] is True

    # …the operator approves THAT card, and nothing else happens (no bridge minted)
    OperatorDecisionQueue(vault).resolve(card.id, choice="Approve once")
    assert not (store._load().get("resume_pending") or {}), "fixture: no bridge exists"

    _run(sb, tool, NASTY, _command_gate_resolved=dedup)

    assert sentinel.exists(), (
        "the operator approved the follow-up card for THIS class and THESE params — "
        "the call must run")
    assert _peek(store, sig, NASTY) is None, "single-use: spent by the call it ran"
    assert _pending(vault) == [], "the approved card is resolved, not re-posted"


# ═══ the scope check, one condition at a time ════════════════════════════════
#
# Each of these mints a resolution that is a follow-up-card approval in every respect
# BUT ONE, so no single test can pass because of a neighbouring condition.

def _seed(tmp_path, monkeypatch, *, card_ctx_patch=None, choice="Approve once",
          pending_params=NASTY, sentinel_name="DESTRUCTIVE_BODY_RAN"):
    """Post a real card for NASTY under a pending reclassification, patch its stored
    context, and resolve it — then hand back everything the caller needs to re-enter
    the gate through the resolved-dedup channel."""
    sentinel = tmp_path / sentinel_name
    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    tool, sig = _tool(tmp_path, sentinel)
    store.mark_reclassified(sig, "local_write",
                            args_fingerprint=args_fingerprint(pending_params))
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, pending_params)
    card = _pending(vault)[0]
    OperatorDecisionQueue(vault).resolve_with_context_patch(
        card.id, choice=choice, context_patch=dict(card_ctx_patch or {}))
    return sb, store, tool, sig, vault, sentinel


def test_an_unreclassified_approval_does_not_bypass(tmp_path, monkeypatch):
    """The exact shape of the live bug, isolated: a resolution whose card was NOT the
    follow-up (no ``reclassified`` marker) must not satisfy a reclassified call."""
    sb, store, tool, sig, vault, sentinel = _seed(
        tmp_path, monkeypatch, card_ctx_patch={"reclassified": False})
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY, _command_gate_resolved=f"tool:{sig}")
    assert not sentinel.exists()
    assert _peek(store, sig, NASTY) == "local_write", "not spent by a refused bypass"


def test_an_approval_scoped_to_another_class_does_not_bypass(tmp_path, monkeypatch):
    """A follow-up approval is a decision about ONE classification. The operator
    approved ``net_mutate``; the call in front of the gate is scored ``local_write``."""
    sb, store, tool, sig, vault, sentinel = _seed(
        tmp_path, monkeypatch,
        card_ctx_patch={"reclassified": True, "assigned_class": "net_mutate"})
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY, _command_gate_resolved=f"tool:{sig}")
    assert not sentinel.exists()
    assert _peek(store, sig, NASTY) == "local_write"


def test_an_approval_for_different_parameters_does_not_bypass(tmp_path, monkeypatch):
    """The substitution vector, on this channel. The dedup key is params-INDEPENDENT,
    so an approval granted for one call is offered to the gate for a completely
    different one on the same tool body."""
    sb, store, tool, sig, vault, sentinel = _seed(
        tmp_path, monkeypatch,
        card_ctx_patch={"reclassified": True, "assigned_class": "local_write",
                        "args_fingerprint": args_fingerprint(OTHER_NASTY)})
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY, _command_gate_resolved=f"tool:{sig}")
    assert not sentinel.exists()
    assert _peek(store, sig, NASTY) == "local_write"


def test_an_approval_with_no_fingerprint_does_not_bypass(tmp_path, monkeypatch):
    """An ABSENT fingerprint matches NOTHING — there is deliberately no "both empty,
    therefore equal" case, mirroring ``_reclassification_applies``."""
    sb, store, tool, sig, vault, sentinel = _seed(
        tmp_path, monkeypatch,
        card_ctx_patch={"reclassified": True, "assigned_class": "local_write",
                        "args_fingerprint": ""})
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY, _command_gate_resolved=f"tool:{sig}")
    assert not sentinel.exists()
    assert _peek(store, sig, NASTY) == "local_write"


# ═══ the ordinary channel is untouched ═══════════════════════════════════════

def test_an_ordinary_approve_once_still_bypasses_with_nothing_pending(
        tmp_path, monkeypatch):
    """The scope check is conditional on a PENDING reclassification, nothing more.
    Without one, the historical one-shot behaves exactly as it always has."""
    sentinel = tmp_path / "BODY_RAN"
    vault = _MemVault(root=tmp_path)
    sb, store = _sandbox(tmp_path, vault)
    tool, sig = _tool(tmp_path, sentinel)

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, BENIGN)
    card = _pending(vault)[0]
    OperatorDecisionQueue(vault).resolve(card.id, choice="Approve once")

    _run(sb, tool, BENIGN, _command_gate_resolved=f"tool:{sig}")
    assert sentinel.exists()


def test_a_refused_bypass_still_retires_the_stale_row(tmp_path, monkeypatch):
    """The stale one-shot is CONSUMED even though it is refused.

    Leaving it would strand the chat lane: ``_poll_command_choice`` is non-consuming, so
    it would keep returning the same stale choice, the lane would keep re-attempting,
    and the operator's approval of the follow-up card would never be the row the poll
    finds. Retiring it is also correct on the merits — that grant was minted for, and
    already spent by, a different call.
    """
    sb, store, tool, sig, vault, sentinel = _seed(
        tmp_path, monkeypatch, card_ctx_patch={"reclassified": False})
    dedup = f"tool:{sig}"
    assert _resolved_rows(vault, dedup), "fixture precondition"

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, NASTY, _command_gate_resolved=dedup)

    assert _resolved_rows(vault, dedup) == [], "the stale row must be retired"
    assert [d.status for d in vault._decisions.values()
            if d.dedup_key == dedup and d.choice == "Approve once"] == ["consumed"]


# ═══ the predicate's own contract ════════════════════════════════════════════
#
# The integration tests above drive every state the GATE can reach. Two terms of the
# predicate are defence in depth that no reachable gate state exercises, and the
# mutation check says so plainly: strip ``bool(stored_fp) and bool(want_fp)`` and the
# whole integration suite stays green, because a call can only be scored under a
# pending reclassification if ``peek_reclassified`` matched, and that requires a
# NON-EMPTY fingerprint on both sides (``_reclassification_applies``).
#
# Rather than leave a security term with no coverage — or, worse, claim an integration
# test covers it — the predicate is pinned directly at its own boundary. These DO fail
# when the terms are removed.

def _decision(ctx):
    from types import SimpleNamespace
    return SimpleNamespace(choice="Approve once", context=ctx)


def test_the_predicate_refuses_an_empty_fingerprint_on_either_side():
    """An ABSENT fingerprint matches NOTHING. There is deliberately no "both empty,
    therefore equal" case: ``GateDescriptor.from_tool`` never receives parameters, so
    two different calls on one signature render byte-identical cards, and an
    empty==empty match would hand the bypass to whichever call arrived."""
    from systemu.runtime.tool_sandbox import _resolved_decision_is_the_follow_up as ok
    base = {"reclassified": True, "assigned_class": "local_write"}

    # both empty — the case the guard exists for
    assert ok(_decision({**base, "args_fingerprint": ""}),
              pending_class="local_write", fingerprint="") is False
    assert ok(_decision({**base}),
              pending_class="local_write", fingerprint="") is False
    # one side empty
    assert ok(_decision({**base, "args_fingerprint": "abc"}),
              pending_class="local_write", fingerprint="") is False
    assert ok(_decision({**base, "args_fingerprint": ""}),
              pending_class="local_write", fingerprint="abc") is False
    # …and the matching pair is honoured
    assert ok(_decision({**base, "args_fingerprint": "abc"}),
              pending_class="local_write", fingerprint="abc") is True


def test_the_predicate_is_unconditional_with_no_reclassification_pending():
    """The historical channel: no pending class ⇒ no scope to check."""
    from systemu.runtime.tool_sandbox import _resolved_decision_is_the_follow_up as ok
    assert ok(_decision({}), pending_class=None, fingerprint="abc") is True
    assert ok(_decision({}), pending_class="", fingerprint="") is True


def test_the_predicate_fails_closed_on_a_missing_or_odd_context():
    """A decision with no context, or a non-dict one, proves nothing about which card
    it came from — so it cannot satisfy a reclassified call."""
    from types import SimpleNamespace
    from systemu.runtime.tool_sandbox import _resolved_decision_is_the_follow_up as ok
    for dec in (SimpleNamespace(choice="Approve once", context=None),
                SimpleNamespace(choice="Approve once"),
                None):
        assert ok(dec, pending_class="local_write", fingerprint="abc") is False


def test_the_predicate_requires_a_literal_reclassified_marker():
    """Truthy-but-not-True values are refused. The gate stamps a literal ``True`` and
    JSON round-trips it unchanged, so anything else came from somewhere else."""
    from systemu.runtime.tool_sandbox import _resolved_decision_is_the_follow_up as ok
    base = {"assigned_class": "local_write", "args_fingerprint": "abc"}
    for marker in (False, "true", 1, None, "yes"):
        assert ok(_decision({**base, "reclassified": marker}),
                  pending_class="local_write", fingerprint="abc") is False
    assert ok(_decision({**base, "reclassified": True}),
              pending_class="local_write", fingerprint="abc") is True


# ═══ LOW: resolve_with_context_patch reaches the EventBus fast path ══════════

def _capture_events():
    from systemu.interface.event_bus import EventBus
    seen = []
    unsub = EventBus.get().subscribe(lambda ev: seen.append(ev), replay=False)
    return seen, unsub


def test_resolve_with_context_patch_publishes_the_resolved_event(tmp_path):
    """Its sibling ``resolve`` publishes ``operator_decision_resolved``; this one did
    not, so a reclassify (the ONLY caller that resolves a resumable gate through the
    patch path) always missed the in-process fast path and waited on the ~15s
    reconciler poll instead."""
    vault = _MemVault(root=tmp_path)
    q = OperatorDecisionQueue(vault)
    did = q.post(title="t", body="b", options=["Deny", RECLASSIFY_OPTION],
                 context={"kind": "gate", "gate_type": "tool"}, dedup_key="tool:s")
    seen, unsub = _capture_events()
    try:
        q.resolve_with_context_patch(did, choice=RECLASSIFY_OPTION,
                                     context_patch={"assigned_class": "local_write"})
    finally:
        unsub()

    resolved = [e for e in seen if e.get("category") == "operator_decision_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["context"]["decision_id"] == did
    assert resolved[0]["context"]["choice"] == RECLASSIFY_OPTION


def test_both_resolve_paths_publish_the_same_event_shape(tmp_path):
    """One publisher, two callers — so a subscriber cannot need to know which path
    resolved a decision."""
    vault = _MemVault(root=tmp_path)
    q = OperatorDecisionQueue(vault)
    a = q.post(title="a", body="b", options=["Deny", "Approve"], dedup_key="k:a",
               context={"kind": "gate", "gate_type": "tool"})
    b = q.post(title="b", body="b", options=["Deny", "Approve"], dedup_key="k:b",
               context={"kind": "gate", "gate_type": "tool"})
    seen, unsub = _capture_events()
    try:
        q.resolve(a, choice="Approve")
        q.resolve_with_context_patch(b, choice="Approve", context_patch={"x": 1})
    finally:
        unsub()

    events = [e for e in seen if e.get("category") == "operator_decision_resolved"]
    assert len(events) == 2
    assert {k for k in events[0]} == {k for k in events[1]}
    assert {k for k in events[0]["context"]} == {k for k in events[1]["context"]}


# ═══ the queue sibling ═══════════════════════════════════════════════════════

def _mk(vault, dedup, choice, ctx):
    q = OperatorDecisionQueue(vault)
    did = q.post(title="t", body="b", options=["Deny", choice], context=dict(ctx),
                 dedup_key=dedup)
    q.resolve(did, choice=choice)
    return did


def test_consume_resolved_decision_returns_the_context_and_retires_the_row(tmp_path):
    """The gate cannot verify WHICH card a choice came from without the card. The
    sibling exposes the decision; the choice-only method is unchanged for the three
    callers that never needed more."""
    vault = _MemVault(root=tmp_path)
    q = OperatorDecisionQueue(vault)
    did = _mk(vault, "tool:s", "Approve once",
              {"kind": "gate", "gate_type": "tool", "reclassified": True,
               "assigned_class": "local_write", "args_fingerprint": "abc"})

    dec = q.consume_resolved_decision("tool:s")
    assert dec is not None
    assert dec.id == did and dec.choice == "Approve once"
    assert dec.context["assigned_class"] == "local_write"
    assert vault.get_decision(did).status == "consumed"
    assert q.consume_resolved_decision("tool:s") is None, "single-use"


def test_consume_resolved_choice_is_unchanged_for_its_existing_callers(tmp_path):
    """``mcp/dispatch``, ``shadow_runtime`` sampling and ``_maybe_gate_command`` keep
    the string contract: newest resolved choice, consumed, then None."""
    vault = _MemVault(root=tmp_path)
    q = OperatorDecisionQueue(vault)
    _mk(vault, "mcp:srv:tool", "Approve once", {"kind": "gate"})
    assert q.consume_resolved_choice("mcp:srv:tool") == "Approve once"
    assert q.consume_resolved_choice("mcp:srv:tool") is None
    assert q.consume_resolved_choice("") is None
    assert q.consume_resolved_choice("nothing:here") is None
