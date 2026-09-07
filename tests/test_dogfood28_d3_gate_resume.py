"""Dogfood 0.10.28 D3 — approving a ``Run tool:`` gate must RESUME the parked run.

THE WITNESSED DEFECT
--------------------
A forged tool's first run parked on the per-tool action gate ("unclassifiable
effect - dangerous-until-proven"; card ``Run tool: pdf_encrypt``). Clicking
**Approve once** logged ``Resolved: Run tool: pdf_encrypt`` -- and then nothing:
no resume, no follow-up event, the task sat parked for 15+ minutes.

ROOT CAUSE
----------
``resume_on_decision._dispatch_resume`` refuses EVERY decision that does not
carry a ``chat_submission_id``::

    if not dctx.get("chat_submission_id"):
        return False

That guard predates gate resume entirely (it was written for the chat-lane
``structured_question`` answer, whose reply is stashed into a chat-lane
snapshot). Since v0.10.21 a command/tool gate stamps its OWN resume coords --
``execution_id`` + ``activity_id`` + ``shadow_id`` -- into the decision context
and needs nothing at all from the chat lane: ``supervisor.submit(...,
chat_submission_id=None)`` is a perfectly good workflow-lane re-dispatch.

So any run that was NOT dispatched from chat -- the forge heal sweep
(``tool_service.heal_activities_for_tool`` -> ``decide_shadow`` -> submit with
no chat_submission_id), a scheduled run, a recovery re-dispatch -- parks on the
tool gate with no ``chat_submission_id``, and resolving the card:

  * reports "Operator decision recorded (Approve once); run unblocked." (false),
  * marks NO resume bridge, schedules NO re-dispatch, writes NO event,
  * and is skipped by the cross-process reconciler forever
    (``scheduler/jobs.py`` carries the same pre-filter).

Resolve into silence. These tests drive the REAL objects end to end: a real
Vault under tmp, the real ``ToolSandbox`` parking function, the real
``OperatorDecisionQueue.resolve`` + ``inbox.resolve_gate`` pair the dashboard
runs, the real EventBus subscriber, and a real ``CommandApprovalStore``. The
only fakes are the two seams the ruling allows: the dispatcher (Supervisor) and
the tool executor (the sandbox backend).
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from systemu.core.models import (
    Activity, ActivityStatus, Shadow, Tool, ToolType,
)
from systemu.runtime.command_approvals import CommandApprovalStore, tool_signature


# ── real-object harness ──────────────────────────────────────────────────────

TOOL_NAME = "pdf_encrypt"
ACT_ID = "act_dogfood28"
SHADOW_ID = "sh_dogfood28"
EXEC_ID = "exec_dogfood28"


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    root = tmp_path / "vault"
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions", "decisions"):
        (root / sub).mkdir(parents=True)
        (root / sub / "index.json").write_text("[]", encoding="utf-8")
    return Vault(str(root))


def _forged_tool(tmp_path):
    """A forged tool with NO declared effect_tags -- the operator's exact case.

    Empty tags => EffectTag.UNKNOWN => evaluate_action returns REQUIRE_APPROVAL
    with "unclassifiable effect - gated (dangerous-until-proven)", the verbatim
    reason on the operator's card.
    """
    impl_dir = tmp_path / "impls"
    impl_dir.mkdir(parents=True, exist_ok=True)
    impl = impl_dir / f"{TOOL_NAME}.py"
    impl.write_text("def run(**kw):\n    return {'success': True}\n", encoding="utf-8")
    return Tool(
        id=f"tool_{TOOL_NAME}", name=TOOL_NAME, description="encrypt a pdf",
        tool_type=ToolType.PYTHON_FUNCTION,
        implementation_path=str(impl), effect_tags=[], version=1,
    ), impl


def _seed_activity(vault):
    vault.save_activity(Activity(
        id=ACT_ID, name="encrypt the report", scroll_id="scr_1",
        required_tool_ids=[f"tool_{TOOL_NAME}"], status=ActivityStatus.ASSIGNED,
    ))


class _Dispatcher:
    """The dispatcher SEAM. Records every re-dispatch the resume rail schedules."""

    def __init__(self):
        self.submits = []

    def submit(self, activity_id, shadow_id, **kw):
        self.submits.append({
            "activity_id": activity_id,
            "shadow_id": shadow_id,
            "resume_from_execution_id": kw.get("resume_from_execution_id"),
            "chat_submission_id": kw.get("chat_submission_id"),
        })
        return "sub_resumed"


class _Backend:
    """The tool EXECUTOR seam. Never runs a subprocess; records the calls."""

    def __init__(self):
        self.calls = []

    async def execute(self, impl_path, params_json, timeout=None, extra_packages=None):
        from systemu.runtime.tool_sandbox import ToolResult
        self.calls.append(str(impl_path))
        return ToolResult(success=True, parsed={"success": True})


class _EventSink:
    """Subscribes to the production EventBus and keeps every published event."""

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


def _bind_approval_store(monkeypatch, tmp_path):
    """Point every ``init_default_store(Path("data"))`` call at a REAL store in
    tmp, so nothing writes into the worktree."""
    import systemu.runtime.command_approvals as ca
    store = CommandApprovalStore(tmp_path / "command_approvals.json")
    monkeypatch.setattr(ca, "init_default_store", lambda p: store)
    return store


def _bridge_marked(store, sig) -> bool:
    """Non-consuming witness that a single-use resume bridge is on record.

    Reads the durable file, so it neither spends the one-shot nor trusts an
    in-memory cache. A store that was never written at all reads as "no bridge".
    """
    if not store.path.exists():
        return False
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    return sig in (raw.get("resume_pending") or {})


def _sandbox(vault, tmp_path, store, backend):
    from systemu.runtime.tool_sandbox import ToolSandbox
    sb = ToolSandbox(str(vault.root), vault=vault, command_approvals=store)
    sb._backend = backend
    return sb


def _park_on_tool_gate(sb, tool, impl, *, chat_submission_id=None):
    """Park EXACTLY the way the forge/heal lane parks: run the production
    ``execute_tool`` chokepoint with the run-coord contextvars set and NO
    chat_submission_id (a heal-sweep dispatch is not a chat submission).

    Returns the posted decision id.
    """
    from systemu.approval.exceptions import PendingOperatorDecision
    from systemu.runtime import chat_submission_ctx as ctx

    tokens = [
        ctx.set_activity_id(ACT_ID),
        ctx.set_shadow_id(SHADOW_ID),
        ctx.set_execution_id(EXEC_ID),
        ctx.set_chat_submission_id(chat_submission_id),
    ]
    setters = [ctx.set_activity_id, ctx.set_shadow_id, ctx.set_execution_id,
               ctx.set_chat_submission_id]
    try:
        with pytest.raises(PendingOperatorDecision) as excinfo:
            asyncio.run(sb.execute_tool(str(impl), {"path": "report.pdf"}, tool=tool))
    finally:
        for setter, token in zip(setters, tokens):
            setter(None, reset_token=token)
    return excinfo.value.decision_id


def _resolve_like_the_dashboard(vault, dec_id, choice):
    """The production resolve pair the Inbox click runs
    (``inbox_page._resolve_and_execute_gate``)."""
    from systemu.approval.decision_queue import OperatorDecisionQueue
    from systemu.interface.command.inbox import resolve_gate
    resolved = OperatorDecisionQueue(vault).resolve(dec_id, choice=choice)
    return resolve_gate(resolved, vault=vault)


@pytest.fixture
def rail(monkeypatch, tmp_path, vault):
    """A registered resume rail: real EventBus subscriber -> _dispatch_resume,
    real approval store, fake dispatcher, fake executor, event sink."""
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    store = _bind_approval_store(monkeypatch, tmp_path)
    dispatcher = _Dispatcher()
    backend = _Backend()
    sink = _EventSink().start()
    unsub = rod.register(vault, dispatcher, data_dir=str(tmp_path / "data"))
    try:
        yield {"store": store, "dispatcher": dispatcher, "backend": backend,
               "sink": sink}
    finally:
        unsub()
        sink.stop()
        rod._handled.clear()


# ── 1. THE REPRO: Approve once on a non-chat run resumes ─────────────────────

def test_approve_once_on_a_non_chat_run_resumes_the_parked_run(
        rail, vault, tmp_path):
    """THE DEFECT. A forged tool parks on the action gate in a run the forge heal
    sweep dispatched (no chat_submission_id). Approve once must:

      (a) mark the single-use resume bridge for the stamped signature,
      (b) schedule a re-dispatch of THAT activity at the dispatcher seam,
      (c) let the resumed tool call consume the bridge and proceed,
      (d) write a follow-up event naming the resume.
    """
    _seed_activity(vault)
    tool, impl = _forged_tool(tmp_path)
    sb = _sandbox(vault, tmp_path, rail["store"], rail["backend"])

    dec_id = _park_on_tool_gate(sb, tool, impl)          # chat_submission_id=None

    decision = vault.get_decision(dec_id)
    assert decision.title == f"Run tool: {TOOL_NAME}"
    assert decision.context.get("chat_submission_id") in (None, "")
    sig = decision.context["tool_signature"]
    assert sig == tool_signature(TOOL_NAME, sb._tool_body_hash(tool), [],
                                 host_class="")

    _resolve_like_the_dashboard(vault, dec_id, "Approve once")

    # (a) the bridge is marked for THAT signature
    assert _bridge_marked(rail["store"], sig) is True, (
        "no single-use resume bridge was recorded for the approved signature")

    # (b) a re-dispatch of that activity was scheduled
    assert rail["dispatcher"].submits == [{
        "activity_id": ACT_ID, "shadow_id": SHADOW_ID,
        "resume_from_execution_id": EXEC_ID, "chat_submission_id": None,
    }]

    # (d) a follow-up event names the resume
    assert any("Resumed after your approval" in m and TOOL_NAME in m
               for m in rail["sink"].messages()), (
        f"no resume event was written; saw {rail['sink'].messages()}")

    # (c) the resumed tool call consumes the bridge and PROCEEDS
    result = asyncio.run(sb.execute_tool(str(impl), {"path": "report.pdf"},
                                         tool=tool))
    assert result.success is True
    assert rail["backend"].calls == [str(impl.resolve())]
    assert _bridge_marked(rail["store"], sig) is False, "bridge was not consumed"


# ── 2. Always allow: resumes AND persists the standing approval ──────────────

def test_always_allow_on_a_non_chat_run_resumes_and_persists(rail, vault, tmp_path):
    _seed_activity(vault)
    tool, impl = _forged_tool(tmp_path)
    sb = _sandbox(vault, tmp_path, rail["store"], rail["backend"])
    dec_id = _park_on_tool_gate(sb, tool, impl)
    sig = vault.get_decision(dec_id).context["tool_signature"]

    _resolve_like_the_dashboard(vault, dec_id, "Always allow")

    assert rail["store"].is_approved(sig) is True        # standing
    assert rail["dispatcher"].submits and \
        rail["dispatcher"].submits[0]["activity_id"] == ACT_ID
    assert any("Resumed after your approval" in m for m in rail["sink"].messages())

    result = asyncio.run(sb.execute_tool(str(impl), {"path": "report.pdf"},
                                         tool=tool))
    assert result.success is True
    assert rail["backend"].calls == [str(impl.resolve())]


# ── 3. Deny: no resume, readable terminal state, readable event ──────────────

def test_deny_on_a_non_chat_run_finalizes_and_never_resumes(rail, vault, tmp_path):
    _seed_activity(vault)
    tool, impl = _forged_tool(tmp_path)
    sb = _sandbox(vault, tmp_path, rail["store"], rail["backend"])
    dec_id = _park_on_tool_gate(sb, tool, impl)
    sig = vault.get_decision(dec_id).context["tool_signature"]

    _resolve_like_the_dashboard(vault, dec_id, "Deny")

    assert rail["dispatcher"].submits == []              # no resume
    assert _bridge_marked(rail["store"], sig) is False   # no bridge minted
    # the activity leaves the parked state in a READABLE terminal status
    assert vault.get_activity(ACT_ID).status is ActivityStatus.FAILED
    assert any("Denied" in m and TOOL_NAME in m for m in rail["sink"].messages()), (
        f"the denial was silent; saw {rail['sink'].messages()}")


# ── 4. the cross-process reconciler must not skip a non-chat gate ────────────

def test_reconciler_picks_up_a_resolved_non_chat_gate(monkeypatch, tmp_path, vault):
    """The CLI (`sharing_on decisions resolve`) resolves in ANOTHER process, so the
    in-process EventBus subscriber never fires and the daemon poll is the only
    rail. Its pre-filter carried the same chat_submission_id refusal."""
    from systemu.runtime import resume_on_decision as rod
    from systemu.scheduler.jobs import reconcile_resolved_stuck_decisions

    rod._handled.clear()
    store = _bind_approval_store(monkeypatch, tmp_path)
    backend = _Backend()
    _seed_activity(vault)
    tool, impl = _forged_tool(tmp_path)
    sb = _sandbox(vault, tmp_path, store, backend)
    dec_id = _park_on_tool_gate(sb, tool, impl)

    # Resolve WITHOUT any in-process subscriber registered (the CLI's situation).
    from systemu.approval.decision_queue import OperatorDecisionQueue
    OperatorDecisionQueue(vault).resolve(dec_id, choice="Approve once")

    dispatcher = _Dispatcher()
    n = reconcile_resolved_stuck_decisions(vault, dispatcher,
                                           data_dir=str(tmp_path / "data"))
    assert n == 1
    assert dispatcher.submits and dispatcher.submits[0]["activity_id"] == ACT_ID


# ── 5. a refused bridge is an EVENT, never silence ───────────────────────────

def test_scope_refusal_of_a_bridge_is_an_event(tmp_path):
    """The bridge's scope check refuses a mismatched redemption. That refusal must
    be readable by the operator, not a bare False."""
    store = CommandApprovalStore(tmp_path / "ca.json")
    sig = "sig_scope_demo"
    store.mark_resume_approved(sig)                      # unscoped bridge
    sink = _EventSink().start()
    try:
        assert store.consume_resume_approved(
            sig, for_reclassification="local_write") is False
    finally:
        sink.stop()
    assert any("resume approval" in m.lower() and "re-ask" in m.lower()
               for m in sink.messages()), (
        f"the scope refusal was silent; saw {sink.messages()}")


def test_absent_bridge_does_not_emit_a_refusal_event(tmp_path):
    """Only a real SCOPE refusal speaks. 'no bridge on record' is the ordinary
    case on every un-approved call and must stay quiet."""
    store = CommandApprovalStore(tmp_path / "ca.json")
    sink = _EventSink().start()
    try:
        assert store.consume_resume_approved("sig_never_minted") is False
    finally:
        sink.stop()
    assert not any("resume approval" in m.lower() for m in sink.messages())


def test_the_refusal_event_is_not_published_while_the_store_lock_is_held(tmp_path):
    """The refusal event must be emitted AFTER the store lock is dropped.

    ``EventBus.publish`` runs every subscriber SYNCHRONOUSLY on the publishing
    thread, and ``CommandApprovalStore._lock`` is a plain ``threading.Lock`` (not
    an RLock). Publishing from INSIDE the critical section therefore wedges any
    subscriber that reads this store -- a live pane rendering approval state is
    exactly such a subscriber -- and because the lock is then never released,
    every later caller of the store wedges behind it too. The gate stops
    answering entirely.

    That is the same failure this branch exists to remove, arriving through the
    fix for it: from the operator's seat a wedged gate and a silent one are
    identical. The pin runs the redemption on its own thread and requires it to
    FINISH, so a regression fails in bounded time instead of hanging the suite.
    """
    store = CommandApprovalStore(tmp_path / "ca.json")
    sig = "sig_lock_reentry"
    store.mark_resume_approved(sig)                      # unscoped bridge

    from systemu.interface.event_bus import EventBus
    reentered = []

    def _subscriber(ev):
        # Only react to the refusal itself, and re-enter the SAME store.
        if "resume approval" in str(ev.get("message") or "").lower():
            reentered.append(store.is_approved(sig))

    unsub = EventBus.get().subscribe(_subscriber, replay=False)
    out = {}

    def _redeem():
        out["result"] = store.consume_resume_approved(
            sig, for_reclassification="local_write")

    worker = threading.Thread(target=_redeem, daemon=True)
    try:
        worker.start()
        worker.join(timeout=10.0)
        assert not worker.is_alive(), (
            "consume_resume_approved never returned: the refusal event is being "
            "published while the store lock is held, so a subscriber that reads "
            "the store deadlocks it")
    finally:
        unsub()

    assert out.get("result") is False
    assert reentered == [False], (
        "the subscriber did not actually re-enter the store, so this test would "
        "not have witnessed the deadlock")


# ── 6. "recorded, but nothing resumed" is also readable, never silence ───────

class _StubDecision:
    """A resolved gate decision with no run behind it. Deliberately NOT built by
    the sandbox: these two branches exist for cards whose run is already gone."""

    def __init__(self, ctx, choice, title="Run tool: pdf_encrypt"):
        self.id, self.context, self.choice, self.title = "dec_gone", ctx, choice, title


class _StubVault:
    def save_decision(self, d):
        pass


def _gate_ctx(**extra):
    base = {"kind": "gate", "gate_type": "tool", "tool_signature": "sigGONE",
            "tool_name": TOOL_NAME, "verdict": "require_approval"}
    base.update(extra)
    return base


def test_gate_with_no_run_to_resume_says_so(monkeypatch, tmp_path):
    """A gate card carrying no execution_id resolves to nothing. That is a valid
    outcome -- and it must be readable, because silence here is the exact shape of
    the bug this branch fixes."""
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    _bind_approval_store(monkeypatch, tmp_path)
    sink = _EventSink().start()
    try:
        # coords present (a real ShadowRuntime park), execution_id absent
        ok = rod._dispatch_resume(
            _StubDecision(_gate_ctx(activity_id=ACT_ID, shadow_id=SHADOW_ID),
                          "Approve once"),
            vault=_StubVault(), supervisor=_Dispatcher(),
            data_dir=str(tmp_path / "data"))
    finally:
        sink.stop()
    assert ok is False
    assert any("not attached to a run" in m and TOOL_NAME in m
               for m in sink.messages()), f"silent; saw {sink.messages()}"


def test_gate_with_no_resume_coords_says_nothing_resumed(monkeypatch, tmp_path):
    """The coords-less rescue records the standing allow and stamps dispatched, but
    it does NOT resume. Say that, rather than letting the card go quiet."""
    from systemu.runtime import execution_snapshot as es
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    store = _bind_approval_store(monkeypatch, tmp_path)
    monkeypatch.setattr(es, "read_snapshot", lambda eid, data_dir=None: None)
    dispatcher = _Dispatcher()
    sink = _EventSink().start()
    try:
        # a legacy CHAT-lane row: it predates the v0.10.21 coord stamp, so the
        # chat_submission_id is the only thing that gets it onto the rail, and
        # there are no coords anywhere to resume from.
        ok = rod._dispatch_resume(
            _StubDecision(_gate_ctx(execution_id="exec_gone",
                                    chat_submission_id="sub_legacy"),
                          "Always allow"),
            vault=_StubVault(), supervisor=dispatcher,
            data_dir=str(tmp_path / "data"))
    finally:
        sink.stop()
    assert ok is True
    assert dispatcher.submits == []                     # genuinely nothing resumed
    assert store.is_approved("sigGONE") is True         # but the allow is recorded
    assert any("nothing resumed" in m and TOOL_NAME in m
               for m in sink.messages()), f"silent; saw {sink.messages()}"


# ── 7. the quick lane stays provably inert ───────────────────────────────────

def test_a_quick_lane_gate_is_still_inert_on_this_rail(monkeypatch, tmp_path):
    """THE BLAST-RADIUS PIN for widening the guard.

    The quick lane (``quick_task``) is NOT a ShadowRuntime run: it stamps an
    ``execution_id`` and nothing else -- no chat_submission_id, and none of the
    activity/shadow carriers -- and it resolves its own gates by BLOCK-POLLING the
    card and re-calling with ``resolved_dedup``. ``_ask_operator_inline``'s
    docstring states the invariant outright: "the Supervisor resume paths
    (resume_on_decision / reconcile) stay provably inert".

    Widening the chat-lane guard to "any gate" would break that: the rescue branch
    would fire, newly persist a standing allow the lane never asked it to, and --
    worst -- tell the operator "the parked run is gone, so nothing resumed" while
    the quick lane was in fact about to run the call. A false statement about what
    just happened is a defect in its own right (DEC-34).

    So the exemption is not "any gate". It is "a gate carrying its OWN resume
    coords", which every ShadowRuntime-lane park has stamped since v0.10.21 and
    which the quick lane never has.
    """
    from systemu.runtime import resume_on_decision as rod
    rod._handled.clear()
    store = _bind_approval_store(monkeypatch, tmp_path)
    dispatcher = _Dispatcher()
    ctx = _gate_ctx(execution_id="quick_abc123")   # quick-lane shape: eid only
    sink = _EventSink().start()
    try:
        ok = rod._dispatch_resume(_StubDecision(ctx, "Always allow"),
                                  vault=_StubVault(), supervisor=dispatcher,
                                  data_dir=str(tmp_path / "data"))
    finally:
        sink.stop()
    assert ok is False, "the quick lane's own block-poll owns this resolution"
    assert dispatcher.submits == []
    assert store.is_approved("sigGONE") is False, (
        "this rail must not start persisting standing allows for the quick lane")
    assert ctx.get("resume_dispatched") is not True
    assert sink.messages() == [], (
        "the quick lane IS about to run the call; claiming nothing resumed is false")


# ── 8. the card copy describes what actually happens ─────────────────────────

def test_tool_card_copy_names_both_approve_paths():
    """The card used to say only what 'Always allow' REMEMBERS, and nothing about
    what either option does to the parked run. Both options run the call NOW and
    the rail picks the run back up -- which is what
    ``test_approve_once_on_a_non_chat_run_resumes_the_parked_run`` above actually
    witnesses. This pins the copy to that behaviour."""
    from systemu.interface.command.gate import GateDescriptor
    d = GateDescriptor.from_tool(
        tool_name=TOOL_NAME, sig="sigX", verdict="require_approval",
        reason="unclassifiable effect - gated (dangerous-until-proven)",
        effect_tags=[])
    what = d.what_approve_does
    assert "Approve once" in what and "runs it now" in what
    assert "Always allow" in what
    assert "remembers this exact tool body + effect set" in what
    assert "picks up on its own" in what
    assert "re-run the task" not in what


def test_parked_run_status_text_does_not_send_the_operator_to_re_run():
    """The task-list text for a gate-blocked run said "a shell command requires
    operator approval ... then re-run the task". It is not always a shell command
    (this branch's repro is a TOOL gate), and following that instruction now starts
    a SECOND run of work the rail is already resuming."""
    from systemu.runtime.supervisor import Supervisor

    class _Pending:
        dedup_key = "tool:sigX"

    res = Supervisor._pending_decision_result(_Pending())
    assert res["status"] == "command_gate_blocked"
    summary = res["final_summary"]
    assert "re-run the task" not in summary
    assert "shell command" not in summary
    assert "picks up" in summary


def test_stuck_question_still_parks_not_blocked():
    """CONTROL for the edit above: a 'stuck:' question keeps its own parked
    classification -- the correction must not smear the two states together."""
    from systemu.runtime.supervisor import Supervisor

    class _Pending:
        dedup_key = "stuck:obj_1"

    assert Supervisor._pending_decision_result(_Pending())["status"] == \
        "suspended_operator_question"
