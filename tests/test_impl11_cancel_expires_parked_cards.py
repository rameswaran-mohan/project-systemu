"""IMPL-11 (queue item 2f) - cancelling a run expires its parked decision cards.

Witnessed gap: the supervisor's CANCELLED finalizer expired the run's durable
WAITS (R-A12a) but not its decision CARDS. A card for a cancelled run stayed in
the Inbox / Needs you / the phone, and answering it could resume a run the
operator had stopped.

Ruling under test: every cancel path calls ONE queue function,
``OperatorDecisionQueue.expire_cards_for_run``, which expires each PENDING card
whose run coordinates (execution_id / activity_id / chat_submission_id - the
coordinates a card context actually carries; resumes of one run share the
activity and the chat submission) belong to the cancelled run, with status
``expired`` and reason ``run cancelled``. Other runs' cards are left alone.

Supervisor cancel entry points exercised (table-driven, through the REAL worker
``_run_shadow_guarded`` -> ``_handle_result``):
  * ``Supervisor.request_cancel(key)``          - the Systemu Chat page Stop;
  * ``Supervisor.request_cancel_by_activity(a)`` - the chat Stop on a queued task;
  * the stuck card's "Cancel run" answer          - the resumed run finalizes
                                                    ``cancelled`` on its own.

Real FileVault under tmp_path; a fake ShadowRuntime (no LLM, no daemon).
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
import threading
import types
from pathlib import Path

import pytest

from systemu.approval.decision_queue import OperatorDecisionQueue


RUN = {"execution_id": "exec_run1", "activity_id": "act_run1",
       "shadow_id": "sh_run1", "chat_submission_id": "2026-10-19T10:00:00.000001"}
OTHER = {"execution_id": "exec_other", "activity_id": "act_other",
         "shadow_id": "sh_other", "chat_submission_id": "2026-10-19T11:00:00.000002"}


# ─────────────────────────────────────────────────────────────── harness ────

def _vault(tmp_path):
    from systemu.vault.vault import Vault
    from systemu.storage.file_vault import FileVault
    return FileVault(Vault(str(tmp_path / "v")))


def _seed_activity(vault, activity_id):
    from systemu.core.models import Activity, ActivityStatus
    vault.save_activity(Activity(id=activity_id, name=activity_id,
                                 scroll_id="scr_" + activity_id,
                                 status=ActivityStatus.ASSIGNED))


def _post_gate(q, coords, *, title="Run tool: thing"):
    ctx = {"kind": "gate", "gate_type": "tool", "tool_name": "thing"}
    ctx.update(coords)
    return q.post(title=title, body="b", options=["Deny", "Approve once"],
                  context=ctx, dedup_key="tool:" + title)


def _status(vault, dec_id):
    return vault.get_decision(dec_id).status


class _FakeRuntime:
    """Stands in for ShadowRuntime. ``mode`` picks how the run ends:
    'wait_cancel' -> loop until the cancel_event is set, then return cancelled;
    'self_cancel' -> return cancelled at once (the stuck card's "Cancel run")."""

    mode = "wait_cancel"
    started = None

    def __init__(self, config, vault):
        pass

    async def execute(self, shadow, activity, *, cancel_event=None, **kw):
        type(self).started.set()
        if type(self).mode == "wait_cancel":
            for _ in range(500):
                if cancel_event is not None and cancel_event.is_set():
                    break
                await asyncio.sleep(0.01)
        return {"status": "cancelled", "execution_id": RUN["execution_id"],
                "summary": "Cancelled by operator"}


def _supervisor(vault, tmp_path):
    from systemu.runtime.supervisor import Supervisor
    sup = Supervisor.__new__(Supervisor)
    sup.vault = vault
    sup.config = None
    sup._running = {}
    sup._running_lock = threading.Lock()
    sup._semaphore = threading.Semaphore(1)
    sup._task_queue = None
    sup._dl_lock = threading.Lock()
    sup._dead_letters = []
    sup._publish = lambda *a, **k: None
    sup._aname = lambda aid: aid
    sup._analyze_failure = lambda *a, **k: None
    sup._update_heartbeat = lambda *a, **k: None
    sup._rate_limiter = types.SimpleNamespace(acquire=lambda n: None)
    sup._snapshot_data_dir = tmp_path / "data"
    return sup


def _run_worker(sup, monkeypatch, *, mode, cancel, runtime=_FakeRuntime):
    """Register a running slot exactly as the dispatcher does, run the REAL
    worker on its own thread, fire ``cancel`` once the run has started."""
    import systemu.runtime.shadow_runtime as sr
    runtime.mode = mode
    runtime.started = threading.Event()
    monkeypatch.setattr(sr, "ShadowRuntime", runtime)
    sup.vault.get_shadow = lambda sid: types.SimpleNamespace(id=sid, name=sid)
    payload = {"activity_id": RUN["activity_id"], "shadow_id": RUN["shadow_id"],
               "submission_id": "sub_1", "retry_count": 0, "origin": "chat",
               "chat_submission_id": RUN["chat_submission_id"]}
    key = f"{payload['activity_id']}_{payload['submission_id']}"
    ev = threading.Event()
    sup._semaphore.acquire()
    sup._running[key] = {"thread": None, "cancel_event": ev, "payload": payload,
                         "started_at": 0.0, "last_heartbeat_at": 0.0,
                         "last_heartbeat_at_mono": 0.0, "status": "starting"}
    t = threading.Thread(target=sup._run_shadow_guarded, args=(key, payload, ev),
                         daemon=True)
    t.start()
    assert runtime.started.wait(5), "fake run never started"
    if cancel is not None:
        assert cancel(sup, key) is True
    t.join(10)
    assert not t.is_alive(), "worker did not finish"


ENTRY_POINTS = [
    pytest.param("wait_cancel", lambda sup, key: sup.request_cancel(key),
                 id="Supervisor.request_cancel"),
    pytest.param("wait_cancel",
                 lambda sup, key: sup.request_cancel_by_activity(RUN["activity_id"]),
                 id="Supervisor.request_cancel_by_activity"),
    pytest.param("self_cancel", None, id="stuck-card-Cancel-run"),
]


# ──────────────────────────────────────────────────── the queue function ────

class TestExpireCardsForRun:
    def test_expires_every_pending_card_of_the_run_and_only_those(self, tmp_path):
        vault = _vault(tmp_path)
        q = OperatorDecisionQueue(vault)
        mine_exec = _post_gate(q, {"execution_id": RUN["execution_id"]}, title="a")
        # an earlier execution of the SAME run (a resume mints a new execution_id
        # but keeps the activity) - matched on the shared activity_id
        mine_resume = _post_gate(q, {"execution_id": "exec_earlier",
                                     "activity_id": RUN["activity_id"]}, title="b")
        mine_chat = _post_gate(q, {"chat_submission_id": RUN["chat_submission_id"]},
                               title="c")
        theirs = _post_gate(q, dict(OTHER), title="d")
        no_coords = q.post(title="unrelated", body="b", options=["Skip", "Apply"],
                           context={"kind": "gate", "gate_type": "recovery"},
                           dedup_key="recovery:x")

        expired = q.expire_cards_for_run(
            execution_id=RUN["execution_id"], activity_id=RUN["activity_id"],
            chat_submission_id=RUN["chat_submission_id"])

        assert sorted(expired) == sorted([mine_exec, mine_resume, mine_chat])
        for did in (mine_exec, mine_resume, mine_chat):
            dec = vault.get_decision(did)
            assert dec.status == "expired"
            assert dec.context["expired_reason"] == "run cancelled"
            assert dec.choice is None
        assert _status(vault, theirs) == "pending"
        assert _status(vault, no_coords) == "pending"
        assert {d.id for d in q.list_pending()} == {theirs, no_coords}

    def test_no_coordinates_expires_nothing(self, tmp_path):
        vault = _vault(tmp_path)
        q = OperatorDecisionQueue(vault)
        did = _post_gate(q, {"activity_id": ""}, title="blank")
        assert q.expire_cards_for_run() == []
        assert q.expire_cards_for_run(execution_id="", activity_id=None) == []
        assert _status(vault, did) == "pending"

    def test_a_non_string_coordinate_off_disk_never_matches(self, tmp_path):
        """DEC-36: the card context is data off disk; its concrete type is pinned
        before it is compared."""
        vault = _vault(tmp_path)
        q = OperatorDecisionQueue(vault)
        did = _post_gate(q, {"activity_id": [RUN["activity_id"]]}, title="list")
        assert q.expire_cards_for_run(activity_id=RUN["activity_id"]) == []
        assert _status(vault, did) == "pending"

    def test_the_exact_parked_card_is_matched_by_id(self, tmp_path):
        """A caller that knows the card the run is parked on (the chat row records
        it) expires it even when the card carries no run coordinates."""
        vault = _vault(tmp_path)
        q = OperatorDecisionQueue(vault)
        did = q.post(title="legacy", body="b", options=["Deny", "Approve once"],
                     context={"kind": "gate", "gate_type": "command"},
                     dedup_key="cmd:legacy")
        assert q.expire_cards_for_run(decision_id=did) == [did]
        assert vault.get_decision(did).context["expired_reason"] == "run cancelled"

    def test_idempotent(self, tmp_path):
        vault = _vault(tmp_path)
        q = OperatorDecisionQueue(vault)
        did = _post_gate(q, {"activity_id": RUN["activity_id"]}, title="once")
        assert q.expire_cards_for_run(activity_id=RUN["activity_id"]) == [did]
        assert q.expire_cards_for_run(activity_id=RUN["activity_id"]) == []

    def test_an_expired_event_is_published(self, tmp_path):
        from systemu.interface.event_bus import EventBus
        vault = _vault(tmp_path)
        q = OperatorDecisionQueue(vault)
        did = _post_gate(q, {"activity_id": RUN["activity_id"]}, title="evt")
        seen = []
        unsub = EventBus.get().subscribe(seen.append, replay=False)
        try:
            q.expire_cards_for_run(activity_id=RUN["activity_id"])
        finally:
            unsub()
        hits = [e for e in seen if e.get("category") == "operator_decision_expired"
                and (e.get("context") or {}).get("decision_id") == did]
        assert hits, "the inline card would never be hidden"
        assert hits[-1]["context"]["reason"] == "run cancelled"


# ───────────────────────────────────────── every supervisor cancel path ─────

class TestSupervisorCancelPathsExpireTheRunsCards:
    @pytest.mark.parametrize("mode,cancel", ENTRY_POINTS)
    def test_cancel_entry_point_expires_the_runs_cards(self, tmp_path, monkeypatch,
                                                       mode, cancel):
        vault = _vault(tmp_path)
        _seed_activity(vault, RUN["activity_id"])
        q = OperatorDecisionQueue(vault)
        mine = _post_gate(q, {"execution_id": RUN["execution_id"],
                              "activity_id": RUN["activity_id"],
                              "chat_submission_id": RUN["chat_submission_id"]},
                          title="mine")
        mine_earlier = _post_gate(q, {"execution_id": "exec_earlier",
                                      "activity_id": RUN["activity_id"]},
                                  title="mine-earlier")
        theirs = _post_gate(q, dict(OTHER), title="theirs")

        sup = _supervisor(vault, tmp_path)
        _run_worker(sup, monkeypatch, mode=mode, cancel=cancel)

        from systemu.core.models import ActivityStatus
        assert vault.get_activity(RUN["activity_id"]).status == ActivityStatus.CANCELLED
        for did in (mine, mine_earlier):
            dec = vault.get_decision(did)
            assert dec.status == "expired", (
                "MUTATION PIN: a cancelled run's card stayed answerable")
            assert dec.context["expired_reason"] == "run cancelled"
        assert _status(vault, theirs) == "pending", "another run's card was touched"

    def test_a_vault_less_supervisor_still_finalizes_the_cancel(self, tmp_path):
        """Best-effort: the card expiry must never break the cancel finalizer."""
        sup = _supervisor(None, tmp_path)
        sup._handle_result({"activity_id": "act_x", "shadow_id": "sh_x"},
                           {"status": "cancelled", "execution_id": "exec_x"})


# ──────────────────────────────────────────────────── reachability pins ─────

def _calls_in(func) -> set:
    """Names of every function/method called inside ``func`` (AST, not text)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


class TestEveryCancelEntryPointReachesTheOneFunction:
    def test_supervisor_cancel_finalizer_calls_the_queue_function(self):
        from systemu.runtime.supervisor import Supervisor
        assert "_expire_cards_on_cancel" in _calls_in(Supervisor._handle_result), (
            "the CANCELLED finalizer no longer withdraws the run's cards")
        assert "expire_cards_for_run" in _calls_in(Supervisor._expire_cards_on_cancel)

    def test_the_supervisor_has_no_ad_hoc_card_expiry(self):
        """The ONE place: nothing in the supervisor expires cards by dedup key."""
        import systemu.runtime.supervisor as mod
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "expire_by_dedup_key" not in src


# ─────────────────────── a cancel the run never observed still cancels it ────

class _ParksAfterCancelRuntime(_FakeRuntime):
    """The operator's Stop lands while the run is mid-iteration; before the loop
    reaches its cancel boundary a gate fires, posts its card and parks the run
    (raises PendingOperatorDecision). ``mode`` here is the vault to post into."""

    vault = None

    async def execute(self, shadow, activity, *, cancel_event=None, **kw):
        from systemu.approval.exceptions import PendingOperatorDecision
        type(self).started.set()
        for _ in range(500):
            if cancel_event is not None and cancel_event.is_set():
                break
            await asyncio.sleep(0.01)
        q = OperatorDecisionQueue(type(self).vault)
        did = _post_gate(q, {"execution_id": RUN["execution_id"],
                             "activity_id": RUN["activity_id"]},
                         title="Run tool: late-gate")
        raise PendingOperatorDecision(decision_id=did, dedup_key="tool:late",
                                      options=["Deny", "Approve once"])


class _FinishesAfterCancelRuntime(_FakeRuntime):
    async def execute(self, shadow, activity, *, cancel_event=None, **kw):
        type(self).started.set()
        for _ in range(500):
            if cancel_event is not None and cancel_event.is_set():
                break
            await asyncio.sleep(0.01)
        return {"status": "success", "execution_id": RUN["execution_id"]}


class TestACancelTheRunDidNotObserveStillCancels:
    def test_a_run_that_parks_after_stop_is_cancelled_and_its_card_expired(
            self, tmp_path, monkeypatch):
        vault = _vault(tmp_path)
        _seed_activity(vault, RUN["activity_id"])
        q = OperatorDecisionQueue(vault)
        theirs = _post_gate(q, dict(OTHER), title="theirs")
        _ParksAfterCancelRuntime.vault = vault
        sup = _supervisor(vault, tmp_path)

        _run_worker(sup, monkeypatch, mode="n/a",
                    cancel=lambda s, key: s.request_cancel(key),
                    runtime=_ParksAfterCancelRuntime)

        from systemu.core.models import ActivityStatus
        assert vault.get_activity(RUN["activity_id"]).status == ActivityStatus.CANCELLED, (
            "the operator's Stop was lost: the run parked instead of stopping")
        late = [d for d in (vault.get_decision(h["id"])
                            for h in vault.load_index("decisions"))
                if d.title == "Run tool: late-gate"]
        assert len(late) == 1
        assert late[0].status == "expired", (
            "MUTATION PIN: the card posted as the Stop landed stayed answerable")
        assert late[0].context["expired_reason"] == "run cancelled"
        assert _status(vault, theirs) == "pending"

    def test_a_run_that_finished_despite_the_stop_stays_completed(
            self, tmp_path, monkeypatch):
        vault = _vault(tmp_path)
        _seed_activity(vault, RUN["activity_id"])
        sup = _supervisor(vault, tmp_path)
        _run_worker(sup, monkeypatch, mode="n/a",
                    cancel=lambda s, key: s.request_cancel(key),
                    runtime=_FinishesAfterCancelRuntime)
        from systemu.core.models import ActivityStatus
        assert vault.get_activity(RUN["activity_id"]).status == ActivityStatus.COMPLETED

    def test_a_run_nobody_cancelled_still_parks(self, tmp_path, monkeypatch):
        """No Stop -> a gate park is a park (the activity is not cancelled and the
        card stays answerable)."""
        vault = _vault(tmp_path)
        _seed_activity(vault, RUN["activity_id"])
        _ParksAfterCancelRuntime.vault = vault
        sup = _supervisor(vault, tmp_path)

        class _ParksNow(_ParksAfterCancelRuntime):
            async def execute(self, shadow, activity, *, cancel_event=None, **kw):
                from systemu.approval.exceptions import PendingOperatorDecision
                type(self).started.set()
                q = OperatorDecisionQueue(type(self).vault)
                did = _post_gate(q, {"execution_id": RUN["execution_id"],
                                     "activity_id": RUN["activity_id"]},
                                 title="Run tool: plain-gate")
                raise PendingOperatorDecision(decision_id=did, dedup_key="tool:plain",
                                              options=["Deny", "Approve once"])

        _run_worker(sup, monkeypatch, mode="n/a", cancel=None, runtime=_ParksNow)
        from systemu.core.models import ActivityStatus
        assert vault.get_activity(RUN["activity_id"]).status != ActivityStatus.CANCELLED
        plain = [d for d in (vault.get_decision(h["id"])
                             for h in vault.load_index("decisions"))
                 if d.title == "Run tool: plain-gate"]
        assert plain and plain[0].status == "pending"


# ───────────────── reachability: every listed entry point -> the one function ─

def _supervisor_edge(name):
    from systemu.runtime.supervisor import Supervisor
    return getattr(Supervisor, name)


# (entry point, chain of (function, callee it must call)) - each hop is an AST
# call edge, so deleting any hop's call turns its row red.
CANCEL_ENTRY_CHAINS = [
    pytest.param([("request_cancel", "set")], id="Supervisor.request_cancel"),
    pytest.param([("request_cancel_by_activity", "request_cancel")],
                 id="Supervisor.request_cancel_by_activity"),
    pytest.param([("_run_shadow_guarded", "_honour_operator_cancel"),
                  ("_run_shadow_guarded", "_handle_result")],
                 id="worker-finalizes-every-signalled-run"),
    pytest.param([("_handle_result", "_expire_cards_on_cancel"),
                  ("_expire_cards_on_cancel", "expire_cards_for_run")],
                 id="CANCELLED-finalizer->expire_cards_for_run"),
]


@pytest.mark.parametrize("chain", CANCEL_ENTRY_CHAINS)
def test_cancel_entry_point_chain_reaches_the_one_function(chain):
    for func_name, callee in chain:
        assert callee in _calls_in(_supervisor_edge(func_name)), (
            f"{func_name} no longer calls {callee} - a cancel path stopped "
            f"reaching expire_cards_for_run")
