"""R1 (spec section 10, queue item 1a) -- a money gate is approved ONCE, under a TYPED
confirmation, and never remembered.

THE WITNESSED GAP (read-only audit of this tree):

  * a tool card whose effect set includes ``money_move`` offered one-click
    "Approve once" AND "Always allow" at risk "medium" on the dashboard
    (``GateDescriptor.from_tool``); only the phone lane refused money cards;
  * nothing in the resolve path asked for anything but a click, so a click on
    "Approve once" minted the resume bridge and re-dispatched the run;
  * the approval store would mint a standing allow for a money signature if
    any caller asked.

THE RULING (R1, the spec wins): a money / irreversible gate is HIGH risk, offers
Deny and "Approve once" only, never a standing approval of any kind (refused at
the store too), and "Approve once" requires the operator to TYPE a phrase that
names the action -- the amount when the arguments carry one, else the tool name.
The phrase is checked server-side in the resolve path; a click without it
resolves nothing and says why.

Everything here drives the REAL objects: a real Vault under tmp, the real
``ToolSandbox`` gate, the real ``OperatorDecisionQueue``, the real resume rail
(EventBus subscriber) and a real ``CommandApprovalStore``. The only fakes are the
two seams the rail tests already use: the dispatcher (Supervisor) and the tool
executor (the sandbox backend). Nothing writes into the worktree.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from systemu.core.models import Activity, ActivityStatus, Tool, ToolType
from systemu.runtime.command_approvals import CommandApprovalStore

TOOL_NAME = "wire_funds"
ACT_ID = "act_r1_money"
SHADOW_ID = "sh_r1_money"
EXEC_ID = "exec_r1_money"
PARAMS = {"amount": 250, "to_account": "ACCT-1"}
PHRASE = "250"


# -- real-object harness -------------------------------------------------------

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    root = tmp_path / "vault"
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"):
        (root / sub).mkdir(parents=True)
        (root / sub / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(root))


def _money_tool(tmp_path, *, name=TOOL_NAME, tags=("money_move",)):
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / f"{name}.py"
    impl.write_text("def run(**kw):\n    return {'success': True}\n", encoding="utf-8")
    return Tool(id=f"tool_{name}", name=name, description="move money",
                tool_type=ToolType.PYTHON_FUNCTION, implementation_path=str(impl),
                effect_tags=list(tags), version=1), impl


def _seed_activity(vault):
    vault.save_activity(Activity(
        id=ACT_ID, name="pay the vendor", scroll_id="scr_1",
        required_tool_ids=[f"tool_{TOOL_NAME}"], status=ActivityStatus.ASSIGNED))


class _Dispatcher:
    def __init__(self):
        self.submits = []

    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append({"activity_id": activity_id, "shadow_id": shadow_id,
                             "resume_from_execution_id": kw.get("resume_from_execution_id")})
        return "sub_resumed"


class _Backend:
    def __init__(self):
        self.calls = []

    async def execute(self, impl_path, params_json, timeout=None, extra_packages=None):
        from systemu.runtime.tool_sandbox import ToolResult
        self.calls.append(json.loads(params_json))
        return ToolResult(success=True, parsed={"success": True})


class _EventSink:
    def __init__(self):
        self.events = []
        self._unsub = None

    def start(self):
        from systemu.interface.event_bus import EventBus
        self._unsub = EventBus.get().subscribe(self.events.append, replay=False)
        return self

    def stop(self):
        if self._unsub is not None:
            self._unsub()

    def messages(self):
        return [str(e.get("message") or "") for e in self.events]


def _bind_store(monkeypatch, tmp_path):
    """Every ``init_default_store(Path("data"))`` resolves to ONE real store in tmp."""
    import systemu.runtime.command_approvals as ca
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)
    return store


def _raw_store(store) -> dict:
    """The durable bytes: neither spends a one-shot nor trusts an in-memory cache."""
    if not store.path.exists():
        return {}
    return json.loads(store.path.read_text(encoding="utf-8"))


def _bridge_marked(store, sig) -> bool:
    return sig in (_raw_store(store).get("resume_pending") or {})


def _sandbox(vault, store, backend):
    from systemu.runtime.tool_sandbox import ToolSandbox
    sb = ToolSandbox(str(vault.root), vault=vault, command_approvals=store)
    sb._backend = backend
    return sb


def _park(sb, tool, impl, params=PARAMS, *, coords=True):
    """Park exactly the way a workflow run parks (run coords in the contextvars);
    ``coords=False`` parks the way the quick lane does (no coords at all)."""
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime import chat_submission_ctx as ctx

    setters, tokens = [], []
    if coords:
        for setter, value in ((ctx.set_activity_id, ACT_ID), (ctx.set_shadow_id, SHADOW_ID),
                              (ctx.set_execution_id, EXEC_ID),
                              (ctx.set_chat_submission_id, None)):
            setters.append(setter)
            tokens.append(setter(value))
    try:
        with pytest.raises(PendingOperatorDecision) as excinfo:
            asyncio.run(sb.execute_tool(str(impl), dict(params), tool=tool))
    finally:
        for setter, token in zip(setters, tokens):
            setter(None, reset_token=token)
    return excinfo.value


def _run(sb, tool, impl, params=PARAMS, *, resolved_dedup=None):
    return asyncio.run(sb.execute_tool(str(impl), dict(params), tool=tool,
                                       _command_gate_resolved=resolved_dedup))


@pytest.fixture
def rail(monkeypatch, tmp_path, vault):
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    store = _bind_store(monkeypatch, tmp_path)
    dispatcher, backend = _Dispatcher(), _Backend()
    sink = _EventSink().start()
    unsub = rod.register(vault, dispatcher, data_dir=str(tmp_path / "data"))
    try:
        yield {"store": store, "dispatcher": dispatcher, "backend": backend, "sink": sink}
    finally:
        unsub()
        sink.stop()
        rod._handled.clear()


def _one_click(vault, dec_id, choice):
    """What every surface that is NOT the Inbox card does: resolve, then run the
    executor (the CLI ``decisions resolve`` path, verbatim)."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.command.inbox import resolve_gate
    resolved = OperatorDecisionQueue(vault).resolve(dec_id, choice=choice)
    return resolve_gate(resolved, vault=vault)


def _resolution_published(rail, dec_id) -> bool:
    """Did ANY resolution of this decision reach the bus? A refused typed confirm
    must record nothing at all -- not resolve-then-reopen."""
    return any(e.get("category") == "operator_decision_resolved"
               and (e.get("context") or {}).get("decision_id") == dec_id
               for e in rail["sink"].events)


def _inbox(vault, dec_id, choice, typed=None):
    """The Inbox card's server-side resolve (``inbox_page`` calls exactly this)."""
    from systemu.interface.command.inbox import resolve_pending_gate
    return resolve_pending_gate(dec_id, choice=choice, vault=vault, typed_confirm=typed)


# -- 1. the card ---------------------------------------------------------------

def test_money_card_offers_only_deny_and_approve_once_at_high_risk(rail, vault, tmp_path):
    """THE GAP, first half: the card offered "Always allow" at risk medium."""
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)

    dec = vault.get_decision(pend.decision_id)
    assert dec.options == ["Deny", "Approve once"], dec.options
    assert list(pend.options) == ["Deny", "Approve once"]
    ctx = dec.context
    assert ctx["risk"] == "high"
    # The irreversible stamp: the card and the resolve path read THIS value.
    assert ctx["irreversible"] is True
    assert ctx["irreversible_effects"] == ["money_move"]
    assert ctx["requires_typed_confirm"] is True
    assert ctx["confirm_phrase"] == PHRASE


def test_a_name_escalated_money_tool_is_a_money_card_too(rail, vault, tmp_path):
    """Untagged ``wire_funds`` scores money_move off the NAME. The derivation reads
    the tags the governor SCORED, never the declared ones."""
    tool, impl = _money_tool(tmp_path, tags=())
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)
    dec = vault.get_decision(pend.decision_id)
    assert dec.options == ["Deny", "Approve once"]
    assert dec.context["irreversible_effects"] == ["money_move"]


def test_money_card_copy_says_what_is_approved_and_why_it_cannot_be_remembered(
        rail, vault, tmp_path):
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)
    ctx = vault.get_decision(pend.decision_id).context
    what = ctx["what_approve_does"]
    assert TOOL_NAME in what and "money_move" in what
    assert "moves money" in what
    assert "cannot be remembered" in what
    assert "Always allow" in what            # names what is NOT offered, and why
    assert f"type {PHRASE} exactly" in what
    assert "Approve once" in what
    what.encode("ascii")                     # ASCII-only verdict string
    assert "250" in str(pend) and "Always allow" not in str(pend)


def test_the_phrase_is_the_tool_name_when_no_amount_is_carried(rail, vault, tmp_path):
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl,
                 params={"to_account": "ACCT-1"})
    assert vault.get_decision(pend.decision_id).context["confirm_phrase"] == TOOL_NAME


# -- 2. the Inbox resolve path (server-side) -----------------------------------

def test_approve_once_without_the_phrase_resolves_nothing_and_says_why(
        rail, vault, tmp_path):
    tool, impl = _money_tool(tmp_path)
    sb = _sandbox(vault, rail["store"], rail["backend"])
    pend = _park(sb, tool, impl)
    sig = vault.get_decision(pend.decision_id).context["tool_signature"]

    res = _inbox(vault, pend.decision_id, "Approve once", typed=None)

    assert res.status.value == "noop"
    assert f"type {PHRASE} exactly" in res.summary.lower()
    assert "nothing ran" in res.summary.lower()
    res.summary.encode("ascii")
    assert res.data["notice_kind"] == "warning"
    dec = vault.get_decision(pend.decision_id)
    assert dec.status == "pending" and dec.choice is None
    assert _resolution_published(rail, pend.decision_id) is False
    assert rail["dispatcher"].submits == []
    assert _bridge_marked(rail["store"], sig) is False
    assert rail["backend"].calls == []


@pytest.mark.parametrize("typed", ["", "25", "2500", "wire_funds", "250.0"])
def test_a_wrong_phrase_resolves_nothing(rail, vault, tmp_path, typed):
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)

    res = _inbox(vault, pend.decision_id, "Approve once", typed=typed)

    assert res.status.value == "noop"
    assert vault.get_decision(pend.decision_id).status == "pending"
    assert _resolution_published(rail, pend.decision_id) is False
    assert rail["dispatcher"].submits == []


def test_a_typed_value_that_is_not_a_plain_str_is_refused(rail, vault, tmp_path):
    """DEC-36: the typed value's concrete type is pinned before any comparison; a str
    subclass whose ``__eq__`` always agrees must not pass."""
    class _Agreeable(str):
        def __eq__(self, other):
            return True

        __hash__ = str.__hash__

    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)
    res = _inbox(vault, pend.decision_id, "Approve once", typed=_Agreeable("nope"))
    assert res.status.value == "noop"
    assert vault.get_decision(pend.decision_id).status == "pending"
    assert _resolution_published(rail, pend.decision_id) is False


def test_always_allow_is_refused_on_a_money_card_even_with_the_phrase(
        rail, vault, tmp_path):
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)
    sig = vault.get_decision(pend.decision_id).context["tool_signature"]

    res = _inbox(vault, pend.decision_id, "Always allow", typed=PHRASE)

    assert res.status.value == "noop"
    assert "cannot be remembered" in res.summary
    assert vault.get_decision(pend.decision_id).status == "pending"
    assert rail["store"].is_approved(sig) is False


def test_the_right_phrase_resumes_through_the_same_bridge_exactly_once(
        rail, vault, tmp_path):
    _seed_activity(vault)
    tool, impl = _money_tool(tmp_path)
    sb = _sandbox(vault, rail["store"], rail["backend"])
    pend = _park(sb, tool, impl)
    sig = vault.get_decision(pend.decision_id).context["tool_signature"]

    res = _inbox(vault, pend.decision_id, "Approve once", typed=PHRASE)

    assert res.status.value == "ok", res.summary
    assert "resuming now" in res.summary
    assert vault.get_decision(pend.decision_id).status == "resolved"
    assert rail["dispatcher"].submits == [{
        "activity_id": ACT_ID, "shadow_id": SHADOW_ID,
        "resume_from_execution_id": EXEC_ID}]
    assert _bridge_marked(rail["store"], sig) is True
    assert rail["store"].is_approved(sig) is False       # never a standing allow

    # the resumed call redeems the bridge and runs -- once
    assert _run(sb, tool, impl).success is True
    assert rail["backend"].calls == [PARAMS]
    # the identical NEXT call is gated again from scratch
    _park(sb, tool, impl)
    assert rail["backend"].calls == [PARAMS]


def test_the_typed_approval_does_not_cover_a_substituted_amount(rail, vault, tmp_path):
    """The operator typed 250. A resumed call that now carries 9999 is a different
    action: the bridge is bound to the call the phrase was typed against."""
    _seed_activity(vault)
    tool, impl = _money_tool(tmp_path)
    sb = _sandbox(vault, rail["store"], rail["backend"])
    pend = _park(sb, tool, impl)
    _inbox(vault, pend.decision_id, "Approve once", typed=PHRASE)

    _park(sb, tool, impl, params={"amount": 9999, "to_account": "ACCT-1"})
    assert rail["backend"].calls == []


# -- 3. the rail: a click from ANY other surface resolves nothing -------------

def test_a_one_click_resolve_from_another_surface_resumes_nothing(rail, vault, tmp_path):
    """THE GAP, second half: ``queue.resolve(.., "Approve once")`` (the CLI, the
    phone, any surface that never asked for the phrase) used to mint the bridge and
    re-dispatch. The rail refuses it, re-opens the card, and says why."""
    _seed_activity(vault)
    tool, impl = _money_tool(tmp_path)
    sb = _sandbox(vault, rail["store"], rail["backend"])
    pend = _park(sb, tool, impl)
    sig = vault.get_decision(pend.decision_id).context["tool_signature"]

    res = _one_click(vault, pend.decision_id, "Approve once")

    assert rail["dispatcher"].submits == []
    assert _bridge_marked(rail["store"], sig) is False
    assert rail["store"].is_approved(sig) is False
    dec = vault.get_decision(pend.decision_id)
    assert dec.status == "pending", "the card must come back for the typed confirm"
    assert "resuming now" not in res.summary
    assert f"type {PHRASE} exactly" in res.summary.lower()
    assert any("Not approved" in m and TOOL_NAME in m and PHRASE in m
               for m in rail["sink"].messages()), rail["sink"].messages()
    assert vault.get_activity(ACT_ID).status is ActivityStatus.ASSIGNED   # not killed
    # and the parked call still does not run
    _park(sb, tool, impl)
    assert rail["backend"].calls == []

    # the SAME card, now approved with the phrase, resumes normally
    res2 = _inbox(vault, pend.decision_id, "Approve once", typed=PHRASE)
    assert res2.status.value == "ok", res2.summary
    assert len(rail["dispatcher"].submits) == 1
    assert _bridge_marked(rail["store"], sig) is True


def test_a_bare_queue_resolve_is_refused_and_reopened_by_the_rail_alone(
        rail, vault, tmp_path):
    """Surfaces that call ONLY ``queue.resolve`` (the phone bridge, the /insights
    card) never run ``resolve_gate``. The rail alone must refuse and re-open."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    _seed_activity(vault)
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)

    OperatorDecisionQueue(vault).resolve(pend.decision_id, choice="Approve once")

    dec = vault.get_decision(pend.decision_id)
    assert dec.status == "pending" and dec.choice is None
    assert dec.context["typed_confirm_refused"]
    assert rail["dispatcher"].submits == []
    assert any("card is back in your Inbox" in m for m in rail["sink"].messages())
    # a replay of the event must not read the re-opened row's empty choice as Deny
    from systemu.runtime import resume_on_decision as rod
    assert rod._dispatch_resume(dec, vault=vault, supervisor=rail["dispatcher"]) is False
    assert vault.get_activity(ACT_ID).status is ActivityStatus.ASSIGNED


def test_deny_still_needs_no_phrase(rail, vault, tmp_path):
    _seed_activity(vault)
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)

    res = _inbox(vault, pend.decision_id, "Deny", typed=None)

    assert res.status.value == "ok"
    assert vault.get_decision(pend.decision_id).status == "resolved"
    assert vault.get_activity(ACT_ID).status is ActivityStatus.FAILED
    assert rail["dispatcher"].submits == []


def test_a_stale_card_offering_always_allow_mints_no_standing_approval(
        rail, vault, tmp_path):
    """A money card posted by an OLDER build still offers "Always allow" (posting
    dedups onto the old pending row). Clicking it must remember nothing, run
    nothing, and the Inbox must not draw the option at all."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.command.inbox import InboxQueue

    _seed_activity(vault)
    sig = "1" * 40
    dec_id = OperatorDecisionQueue(vault).post(
        title=f"Run tool: {TOOL_NAME}", body="tool: wire_funds",
        options=["Deny", "Approve once", "Always allow"],
        context={"kind": "gate", "gate_type": "tool", "risk": "medium",
                 "tool_signature": sig, "tool_name": TOOL_NAME,
                 "verdict": "require_approval", "effect_tags": ["money_move"],
                 "destructive": False, "args_fingerprint": "f" * 40,
                 "execution_id": EXEC_ID, "activity_id": ACT_ID,
                 "shadow_id": SHADOW_ID},
        dedup_key=f"tool:{sig}")

    (_, desc), = [d for d in InboxQueue(vault).list_descriptors() if d[0] == dec_id]
    assert desc.options == ["Deny", "Approve once"]
    assert desc.risk == "high"
    assert desc.confirm_phrase == TOOL_NAME

    _one_click(vault, dec_id, "Always allow")

    assert rail["store"].is_approved(sig) is False
    assert _raw_store(rail["store"]).get("approved", {}) == {}
    assert rail["dispatcher"].submits == []
    assert vault.get_decision(dec_id).status == "pending"


# -- 4. the chat lane (resolved-dedup one-shot) --------------------------------

def test_chat_lane_one_click_approval_does_not_run(rail, vault, tmp_path):
    tool, impl = _money_tool(tmp_path)
    sb = _sandbox(vault, rail["store"], rail["backend"])
    pend = _park(sb, tool, impl, coords=False)

    _one_click(vault, pend.decision_id, "Approve once")

    from systemu.approval.exceptions import PendingOperatorDecision
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, impl, resolved_dedup=pend.dedup_key)
    assert rail["backend"].calls == []


def test_chat_lane_typed_approval_runs_once_and_only_for_that_call(rail, vault, tmp_path):
    from systemu.approval.exceptions import PendingOperatorDecision

    tool, impl = _money_tool(tmp_path)
    sb = _sandbox(vault, rail["store"], rail["backend"])
    pend = _park(sb, tool, impl, coords=False)
    res = _inbox(vault, pend.decision_id, "Approve once", typed=PHRASE)
    assert res.status.value == "ok", res.summary

    # a substituted call on the same (params-independent) dedup key does not cash it
    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, impl, params={"amount": 9999, "to_account": "ACCT-1"},
             resolved_dedup=pend.dedup_key)
    assert rail["backend"].calls == []


def test_chat_lane_typed_approval_runs_the_call_it_was_typed_for(rail, vault, tmp_path):
    tool, impl = _money_tool(tmp_path)
    sb = _sandbox(vault, rail["store"], rail["backend"])
    pend = _park(sb, tool, impl, coords=False)
    _inbox(vault, pend.decision_id, "Approve once", typed=PHRASE)

    assert _run(sb, tool, impl, resolved_dedup=pend.dedup_key).success is True
    assert rail["backend"].calls == [PARAMS]


# -- 5. the store refuses a standing approval of any kind ----------------------

def test_the_store_refuses_to_mint_a_standing_approval_for_a_money_signature(
        rail, vault, tmp_path):
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)
    sig = vault.get_decision(pend.decision_id).context["tool_signature"]
    store = rail["store"]

    assert store.approve(sig) is False              # a value refusal, never a raise
    assert store.is_approved(sig) is False
    assert sig not in (_raw_store(store).get("approved") or {})


def test_the_store_refuses_when_the_caller_names_a_money_effect(tmp_path):
    store = CommandApprovalStore(tmp_path / "ca.json")
    assert store.approve("2" * 40, effect_tags=["net_read", "money_move"]) is False
    assert store.is_approved("2" * 40) is False
    assert store.approve("3" * 40, effect_tags=["net_read"]) is True     # control


def test_the_store_reports_no_standing_approval_for_a_registered_money_signature(
        tmp_path):
    """Every OTHER reader of the store (the capability diff, a CLI listing) must not
    believe a standing allow exists for a signature the gate classified
    irreversible, even when an older file still lists one."""
    store = CommandApprovalStore(tmp_path / "ca.json")
    sig = "4" * 40
    store.path.write_text(json.dumps({"version": 1, "approved": {sig: {
        "approved_at": "2026-01-01T00:00:00+00:00", "approved_by": "operator"}}}),
        encoding="utf-8")
    assert store.is_approved(sig) is True                  # control: as on disk
    assert store.mark_one_shot_only(sig, effects=("money_move",)) is True
    assert store.is_approved(sig) is False
    assert store.is_one_shot_only(sig) is True


def test_a_standing_approval_already_on_disk_does_not_cover_a_money_call(
        monkeypatch, tmp_path, vault):
    """The CONSUMPTION side. An allow written before this rule (older build, a
    hand-edited file) must not run a money call -- the gate cards it anyway."""
    from systemu.runtime.command_approvals import tool_signature
    store = _bind_store(monkeypatch, tmp_path)
    tool, impl = _money_tool(tmp_path)
    backend = _Backend()
    sb = _sandbox(vault, store, backend)
    sig = tool_signature(TOOL_NAME, sb._tool_body_hash(tool), {"money_move"},
                         host_class="undeclared")
    store.path.write_text(json.dumps({"version": 1, "approved": {sig: {
        "approved_at": "2026-01-01T00:00:00+00:00", "approved_by": "operator"}}}),
        encoding="utf-8")

    pend = _park(sb, tool, impl)
    assert vault.get_decision(pend.decision_id).context["tool_signature"] == sig
    assert backend.calls == []


# -- 6. bulk + phone floors (pins: already true, must stay true) --------------

def test_bulk_first_gate_review_excludes_a_money_tool():
    from systemu.runtime.first_gate_review import (apply_bulk_always_allow, build_entry,
                                                   partition_entries)

    class _Store:
        def __init__(self):
            self.approved = []

        def approve(self, sig, **kw):
            self.approved.append(sig)
            return True

    money = build_entry(tool_id="t1", name="pay_invoice", effect_tags=["money_move"],
                        signature="sig::money")
    named = build_entry(tool_id="t2", name="wire_funds", effect_tags=[],
                        signature="sig::named")
    part = partition_entries([money, named])
    assert part.eligible == ()
    store = _Store()
    assert apply_bulk_always_allow([money, named], store=store) == []
    assert store.approved == []


def test_telegram_still_refuses_a_money_card(rail, vault, tmp_path, monkeypatch):
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.messaging.decision_bridge import decision_tag, resolve_from_channel

    monkeypatch.setenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", "4242")
    tool, impl = _money_tool(tmp_path)
    pend = _park(_sandbox(vault, rail["store"], rail["backend"]), tool, impl)
    assert vault.get_decision(pend.decision_id).context["resolution_class"] == "floor"

    for key in ("a1", "a2"):
        outcome, _ = resolve_from_channel(
            decision_tag(pend.decision_id), key, sender_id="4242", channel="telegram",
            queue=OperatorDecisionQueue(vault))
        assert outcome == "REFUSED_TYPED_CONFIRM"
    outcome, _ = resolve_from_channel(
        decision_tag(pend.decision_id), None, sender_id="4242", channel="telegram",
        queue=OperatorDecisionQueue(vault), value=PHRASE)
    assert outcome == "REFUSED_TYPED_CONFIRM"
    assert vault.get_decision(pend.decision_id).status == "pending"


def test_one_typed_confirmation_runs_once_even_when_two_lanes_share_the_card(
        monkeypatch, tmp_path, vault):
    """Two runs parked on one (params-independent) dedup key share ONE card. If the
    chat lane consumes the typed approval first, a late rail (the cross-process
    reconciler) must not ALSO mint a bridge: one confirmation, one run."""
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    store = _bind_store(monkeypatch, tmp_path)
    _seed_activity(vault)
    tool, impl = _money_tool(tmp_path)
    backend = _Backend()
    sb = _sandbox(vault, store, backend)
    pend = _park(sb, tool, impl)                       # a workflow card (coords)
    sig = vault.get_decision(pend.decision_id).context["tool_signature"]

    assert _inbox(vault, pend.decision_id, "Approve once", typed=PHRASE).status.value \
        == "ok"                                        # no rail registered yet
    as_the_event_carried_it = vault.get_decision(pend.decision_id)

    assert _run(sb, tool, impl, resolved_dedup=pend.dedup_key).success is True

    dispatcher = _Dispatcher()
    assert rod._dispatch_resume(as_the_event_carried_it, vault=vault,
                                supervisor=dispatcher) is False
    assert dispatcher.submits == []
    assert _bridge_marked(store, sig) is False
    assert backend.calls == [PARAMS]
    rod._handled.clear()


def test_a_spent_typed_approval_cannot_be_replayed_on_the_chat_lane(rail, vault, tmp_path):
    """The rail spent the confirmation (bridge minted, run resumed, bridge redeemed).
    The decision row stays ``resolved`` forever on that dedup key -- a later
    identical call arriving with ``resolved_dedup`` must not cash it a second time."""
    from systemu.approval.exceptions import PendingOperatorDecision
    _seed_activity(vault)
    tool, impl = _money_tool(tmp_path)
    sb = _sandbox(vault, rail["store"], rail["backend"])
    pend = _park(sb, tool, impl)
    _inbox(vault, pend.decision_id, "Approve once", typed=PHRASE)
    assert _run(sb, tool, impl).success is True            # the resumed run
    assert rail["backend"].calls == [PARAMS]

    with pytest.raises(PendingOperatorDecision):
        _run(sb, tool, impl, resolved_dedup=pend.dedup_key)
    assert rail["backend"].calls == [PARAMS]


def test_a_follow_up_card_reclassified_as_money_needs_the_phrase_too(rail, vault, tmp_path):
    """IMPL-2: a DENY-floored call the operator reclassifies as money_move posts a
    follow-up card. Money is money whatever produced the class: that card is the
    typed-confirm surface too, and a bare click on it resolves nothing."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.runtime.tool_sandbox import args_fingerprint
    _seed_activity(vault)
    tool, impl = _money_tool(tmp_path, name="sync_records", tags=())
    sb = _sandbox(vault, rail["store"], rail["backend"])
    nasty = {"amount": 40, "note": "rm -rf build"}           # destructive-param signal
    deny = _park(sb, tool, impl, params=nasty)
    deny_ctx = vault.get_decision(deny.decision_id).context
    assert deny_ctx["verdict"] == "deny"
    assert "Approve once" not in vault.get_decision(deny.decision_id).options

    OperatorDecisionQueue(vault).resolve(deny.decision_id, choice="Deny")  # retire it
    rail["store"].mark_reclassified(deny_ctx["tool_signature"], "money_move",
                                    args_fingerprint=args_fingerprint(nasty))
    follow = _park(sb, tool, impl, params=nasty)
    dec = vault.get_decision(follow.decision_id)
    assert dec.options == ["Deny", "Approve once"]
    assert dec.context["reclassified"] is True
    assert dec.context["irreversible_effects"] == ["money_move"]
    assert dec.context["confirm_phrase"] == "40"

    OperatorDecisionQueue(vault).resolve(follow.decision_id, choice="Approve once")
    assert vault.get_decision(follow.decision_id).status == "pending"
    assert rail["backend"].calls == []
