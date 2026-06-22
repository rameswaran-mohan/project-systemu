"""v0.9.32 item 3 — operator interrupt/stop control (cooperative cancellation)."""
from __future__ import annotations

import threading
import time

import pytest


# ── C.1: terminal CANCELLED status + activity_completion writer ────────────────

class _FakeVault:
    """Minimal vault stand-in: get_activity / save_activity round-trip."""
    def __init__(self, activity):
        self._activity = activity
        self.saved = None

    def get_activity(self, activity_id):
        assert activity_id == self._activity.id
        return self._activity

    def save_activity(self, activity):
        self.saved = activity


def _make_activity():
    from systemu.core.models import Activity, ActivityStatus
    return Activity(id="act_c1", name="x", scroll_id="scr_c1",
                    status=ActivityStatus.ASSIGNED)


def test_activity_status_cancelled_exists():
    from systemu.core.models import ActivityStatus
    assert ActivityStatus.CANCELLED.value == "cancelled"


def test_mark_activity_failed_with_cancelled_status_persists_cancelled():
    from systemu.core.models import ActivityStatus
    from systemu.runtime.activity_completion import mark_activity_failed
    act = _make_activity()
    vault = _FakeVault(act)
    ok = mark_activity_failed(vault, act.id, status="cancelled",
                              summary="Cancelled by operator")
    assert ok is True
    assert vault.saved is act
    assert vault.saved.status == ActivityStatus.CANCELLED


def test_mark_activity_failed_default_status_still_failed():
    from systemu.core.models import ActivityStatus
    from systemu.runtime.activity_completion import mark_activity_failed
    act = _make_activity()
    vault = _FakeVault(act)
    assert mark_activity_failed(vault, act.id) is True
    assert vault.saved.status == ActivityStatus.FAILED


# ── C.2: Supervisor.request_cancel (set the cancel_event ONLY) ─────────────────

def _bare_supervisor():
    """A Supervisor with just the cancel-relevant state, no daemon/queue/vault."""
    from systemu.runtime.supervisor import Supervisor
    sup = Supervisor.__new__(Supervisor)
    sup._running = {}
    sup._running_lock = threading.Lock()
    sup._semaphore = threading.Semaphore(3)
    return sup


def _add_running_entry(sup, key, activity_id):
    ev = threading.Event()
    sup._running[key] = {
        "cancel_event": ev,
        "payload": {"activity_id": activity_id, "shadow_id": "sh_1"},
        "status": "running",
    }
    return ev


def test_request_cancel_sets_event_and_returns_true():
    sup = _bare_supervisor()
    ev = _add_running_entry(sup, "act_1_sub_1", "act_1")
    assert sup.request_cancel("act_1_sub_1") is True
    assert ev.is_set() is True
    assert sup._running["act_1_sub_1"]["status"] == "cancelling"
    assert sup._running["act_1_sub_1"]["cancel_reason"] == "operator"


def test_request_cancel_unknown_key_returns_false():
    sup = _bare_supervisor()
    assert sup.request_cancel("nope") is False


def test_request_cancel_does_not_release_semaphore():
    sup = _bare_supervisor()
    _add_running_entry(sup, "act_1_sub_1", "act_1")
    before = sup._semaphore._value
    sup.request_cancel("act_1_sub_1")
    # Only the worker's finally releases the slot — request_cancel must NOT,
    # or a double-release lets an extra shadow start (spec D3.1 risk).
    assert sup._semaphore._value == before


def test_request_cancel_by_activity_scans_running():
    sup = _bare_supervisor()
    ev = _add_running_entry(sup, "act_42_sub_9", "act_42")
    assert sup.request_cancel_by_activity("act_42") is True
    assert ev.is_set() is True
    assert sup.request_cancel_by_activity("act_unknown") is False


# ── C.3: _handle_result cancelled branch persists + skips post-mortem ──────────

def _supervisor_for_handle_result(monkeypatch, marks, analyzed):
    """Supervisor with _handle_result deps stubbed; records persistence calls."""
    from systemu.runtime import activity_completion
    sup = _bare_supervisor()
    sup.vault = object()
    sup._task_queue = None
    sup._publish = lambda *a, **k: None
    sup._aname = lambda aid: aid
    sup._dead_letters = []
    sup._dl_lock = threading.Lock()

    def _fake_mark_failed(vault, activity_id, *, status="failed", summary=""):
        marks.append((activity_id, status, summary))
        return True
    monkeypatch.setattr(activity_completion, "mark_activity_failed", _fake_mark_failed)
    # _handle_result runs _analyze_failure in a daemon thread (supervisor.py:1276),
    # so the stub signals an Event the failure test waits on — deterministic, not racy.
    sup._analyzed_event = threading.Event()
    def _fake_analyze(payload, result):
        analyzed.append(payload)
        sup._analyzed_event.set()
    monkeypatch.setattr(sup, "_analyze_failure", _fake_analyze)
    return sup


def test_handle_result_cancelled_persists_cancelled_and_skips_postmortem(monkeypatch):
    marks, analyzed = [], []
    sup = _supervisor_for_handle_result(monkeypatch, marks, analyzed)
    payload = {"activity_id": "act_c3", "shadow_id": "sh", "submission_id": "s1"}
    result = {"status": "cancelled", "final_summary": "stopped by operator"}
    sup._handle_result(payload, result)
    assert marks == [("act_c3", "cancelled", "Cancelled by operator")]
    assert analyzed == []   # no LLM post-mortem on an intentional stop (D-6)


def test_handle_result_failure_still_runs_postmortem(monkeypatch):
    marks, analyzed = [], []
    sup = _supervisor_for_handle_result(monkeypatch, marks, analyzed)
    payload = {"activity_id": "act_c3b", "shadow_id": "sh",
               "submission_id": "s2", "retry_count": 99}
    result = {"status": "failure", "error": "boom"}
    sup._handle_result(payload, result)
    # post-mortem runs in a daemon thread — wait for it deterministically.
    assert sup._analyzed_event.wait(timeout=5.0), "post-mortem not invoked within 5s"
    assert analyzed == [payload]   # genuine failure DOES get the post-mortem


# ── C.4: chat_task_registry (process-global chat_ts -> Event) ──────────────────

def test_chat_registry_register_returns_event_and_cancel_sets_it():
    from systemu.runtime import chat_task_registry as reg
    ts = "2026-06-15T10:00:00"
    reg.unregister(ts)  # clean slate (process-global)
    ev = reg.register(ts)
    assert isinstance(ev, threading.Event)
    assert ev.is_set() is False
    assert reg.request_cancel(ts) is True
    assert ev.is_set() is True
    reg.unregister(ts)


def test_chat_registry_request_cancel_unknown_returns_false():
    from systemu.runtime import chat_task_registry as reg
    assert reg.request_cancel("never-registered-ts") is False


def test_chat_registry_unregister_removes_entry():
    from systemu.runtime import chat_task_registry as reg
    ts = "2026-06-15T11:00:00"
    reg.register(ts)
    reg.unregister(ts)
    # After unregister the id is gone — a later cancel is a default-deny no-op.
    assert reg.request_cancel(ts) is False


def test_chat_registry_register_same_ts_is_idempotent():
    from systemu.runtime import chat_task_registry as reg
    ts = "2026-06-15T12:00:00"
    reg.unregister(ts)
    ev1 = reg.register(ts)
    ev2 = reg.register(ts)
    assert ev1 is ev2   # re-register returns the SAME live Event, never orphans one
    reg.unregister(ts)


# ── C.5: run_quick_task honors a pre-set cancel_event at the loop top ──────────

def test_run_quick_task_preset_cancel_returns_cancelled_without_iterating():
    from systemu.pipelines.quick_task import run_quick_task

    calls = {"n": 0}

    def _spy_llm(*, system, user, config):
        calls["n"] += 1
        return {"action": "ANSWER", "answer_md": "should never run", "completed": True}

    class _V:
        root = None
        def load_index(self, _):
            return []

    ev = threading.Event()
    ev.set()   # already cancelled before the first iteration
    res = run_quick_task("find a spa", config=None, vault=_V(),
                         llm_json=_spy_llm, sandbox=object(),
                         cancel_event=ev)
    assert res.status == "cancelled"
    assert calls["n"] == 0   # the loop body never ran a single LLM call


def test_run_quick_task_no_cancel_event_runs_normally():
    from systemu.pipelines.quick_task import run_quick_task

    def _answer_llm(*, system, user, config):
        return {"action": "ANSWER", "answer_md": "done", "completed": True}

    class _V:
        root = None
        def load_index(self, _):
            return []

    res = run_quick_task("hi", config=None, vault=_V(),
                         llm_json=_answer_llm, sandbox=object())
    assert res.status == "success"


# ── C.6: run_direct_task forwards cancel_event + writes a cancelled branch ─────

def test_run_direct_task_passes_cancel_event_and_writes_cancelled(monkeypatch):
    """Inline-route test (per plan Step-1 note): stub the upstream pipeline
    stages so run_direct_task reaches the sync execute call, then assert the
    cancel_event is forwarded into runtime.execute and the cancelled
    chat-history status is persisted."""
    import systemu.pipelines.direct_task as dt

    seen = {}

    class _FakeRuntime:
        def __init__(self, config, vault):
            pass
        async def execute(self, shadow, activity, **kwargs):
            seen["cancel_event"] = kwargs.get("cancel_event")
            return {"status": "cancelled",
                    "final_summary": "Shadow cancelled at iteration 3.",
                    "execution_id": "exec_x"}

    hist = {}

    class _Activity:
        id = "act_1"
        scroll_id = "scr_1"
        required_tool_ids: list = []
        origin = ""

    class _Scroll:
        id = "scr_1"
        intent = "x"

    class _Shadow:
        id = "sh_1"
        name = "Generalist"

    activity, scroll, shadow = _Activity(), _Scroll(), _Shadow()

    class _Vault:
        def append_chat_history(self, e): pass
        def update_chat_history_entry(self, ts, fields): hist.update(fields)
        def save_activity(self, a): pass
        def get_tool(self, tid): raise KeyError(tid)

    # Stub the upstream pipeline stages (each imported lazily inside the
    # function via `from <mod> import <name>`) so we reach the execute seam.
    monkeypatch.setattr("systemu.interface.notifications.set_vault",
                        lambda *a, **k: None)
    monkeypatch.setattr("systemu.pipelines.activity_extractor.init_pipeline",
                        lambda *a, **k: None)
    monkeypatch.setattr("systemu.pipelines.scroll_refiner.refine_from_text",
                        lambda *a, **k: scroll)
    monkeypatch.setattr("systemu.pipelines.activity_extractor.extract_and_process",
                        lambda *a, **k: activity)
    monkeypatch.setattr("systemu.pipelines.shadow_decision.decide_shadow",
                        lambda *a, **k: shadow)
    monkeypatch.setattr("systemu.runtime.shadow_runtime.ShadowRuntime", _FakeRuntime)
    monkeypatch.setattr(dt, "_maybe_trigger_fact_extraction",
                        lambda *a, **k: None, raising=False)

    # Own the coroutine runner so this test is order-independent (some e2e tests
    # patch _run_coroutine; binding it here makes the _FakeRuntime coroutine
    # actually execute and return the cancelled result regardless).
    import asyncio as _asyncio
    monkeypatch.setattr(dt, "_run_coroutine",
                        lambda coro: _asyncio.new_event_loop().run_until_complete(coro))

    ev = threading.Event()
    out = dt.run_direct_task("x", None, _Vault(), cancel_event=ev)
    assert seen["cancel_event"] is ev
    assert hist.get("status") == "cancelled"
    assert out is activity


# ── C.7: chat lane registers a cancel Event and forwards it to the lane ────────

def test_submit_quick_task_forwards_cancel_event_from_registry(monkeypatch):
    """The chat lane registers an Event for the chat ts and passes it into the
    quick lane — the contract chat_page._run relies on."""
    import systemu.pipelines.quick_task as qt
    from systemu.runtime import chat_task_registry as reg

    captured = {}

    def _fake_run_quick_task(prompt, config, vault, **kwargs):
        captured["cancel_event"] = kwargs.get("cancel_event")
        return qt.QuickResult(status="success", answer_md="ok")

    monkeypatch.setattr(qt, "run_quick_task", _fake_run_quick_task)

    class _V:
        def append_chat_history(self, e): self.ts = e["ts"]
        def update_chat_history_entry(self, ts, f): pass

    v = _V()
    # Simulate what chat_page._run does: register an Event under a ts, pass it in.
    ts = "2026-06-15T13:00:00"
    ev = reg.register(ts)
    try:
        qt.submit_quick_task("hi", None, v, cancel_event=ev)
    finally:
        reg.unregister(ts)
    assert captured["cancel_event"] is ev
    assert reg.request_cancel(ts) is False   # unregistered in finally


# ── C.8: Stop-button click handlers call the right cancel API ─────────────────

def test_systemu_chat_stop_handler_calls_supervisor_request_cancel(monkeypatch):
    from systemu.interface.pages import systemu_chat as sc
    calls = []

    class _FakeSup:
        def request_cancel(self, key):
            calls.append(key)
            return True

    monkeypatch.setattr("systemu.runtime.supervisor.Supervisor.get",
                        classmethod(lambda cls: _FakeSup()))
    handler = sc._make_stop_handler("act_7_sub_3")
    handler()
    assert calls == ["act_7_sub_3"]


def test_chat_page_stop_handler_calls_chat_registry_request_cancel(monkeypatch):
    from systemu.interface.pages import chat_page as cp
    from systemu.runtime import chat_task_registry as reg
    calls = []
    monkeypatch.setattr(reg, "request_cancel", lambda ts: calls.append(ts) or True)
    handler = cp._make_chat_stop_handler("2026-06-15T14:00:00")
    handler()
    assert calls == ["2026-06-15T14:00:00"]


# ── C.9: ReAct-loop cancellation gate guard (no code change; tripwire) ─────────

def test_react_loop_has_cancel_gate_returning_cancelled():
    """Tripwire: the ReAct loop must keep a cancel_event.is_set() gate that
    returns status='cancelled' (shadow_runtime.py:~2677). Guards against a
    refactor silently dropping cooperative cancel (spec item 3, D-5)."""
    import inspect
    from systemu.runtime.shadow_runtime import ShadowRuntime
    src = inspect.getsource(ShadowRuntime.execute)
    assert "cancel_event is not None and cancel_event.is_set()" in src
    assert '"status":        "cancelled"' in src or '"status": "cancelled"' in src


def test_execute_signature_accepts_cancel_event():
    import inspect
    from systemu.runtime.shadow_runtime import ShadowRuntime
    params = inspect.signature(ShadowRuntime.execute).parameters
    assert "cancel_event" in params


# ── REGRESSION (review FIX 4) — cancelled quick result publishes at WARNING ─────

def test_run_quick_task_cancelled_publishes_at_warning(monkeypatch):
    """An intentional operator interrupt must not be logged as an ERROR — the
    quick lane's _finish level map maps 'cancelled' to WARNING (matches the
    supervisor's cancelled publish level)."""
    import systemu.pipelines.quick_task as qt

    levels = []
    monkeypatch.setattr(qt, "_publish",
                        lambda level, message, details=None: levels.append(level))

    class _V:
        root = None
        def load_index(self, _): return []

    ev = threading.Event()
    ev.set()   # cancel before the first iteration → status='cancelled'
    res = qt.run_quick_task("x", config=None, vault=_V(),
                            llm_json=lambda **k: {}, sandbox=object(),
                            cancel_event=ev)
    assert res.status == "cancelled"
    assert levels == ["WARNING"]   # NOT "ERROR"


# ── REGRESSION (review FIX 1) — chat-lane Stop button actually cancels ──────────
# The MAJOR bug: the cancel Event was registered under a `task_ts` generated in
# chat_page._run, but the chat-history entry id was generated INDEPENDENTLY inside
# the lane (direct: utcnow().isoformat() naive-UTC µs; quick:
# datetime.now().isoformat(timespec="seconds") local-second). So the per-entry
# Stop button (request_cancel(entry["ts"])) never matched the registry key —
# direct NEVER, quick only within the same wall-clock second. Fix: one canonical
# ts threaded as `chat_ts=` serves as BOTH the registry key and the entry id.
# These tests drive the real path chat_page._run uses and assert that
# request_cancel(<the entry ts the Stop button reads>) is True for BOTH lanes.

def test_quick_lane_stop_button_cancels_via_canonical_chat_ts(monkeypatch):
    """Quick lane: register under canonical ts, run submit_quick_task(chat_ts=ts),
    then assert the Stop button (request_cancel of the appended entry's ts) works."""
    import systemu.pipelines.quick_task as qt
    from systemu.runtime import chat_task_registry as reg

    # Stub the loop so the test is fast/keyless; capture the cancel_event reached.
    seen = {}

    def _fake_run_quick_task(prompt, config, vault, **kwargs):
        seen["cancel_event"] = kwargs.get("cancel_event")
        return qt.QuickResult(status="success", answer_md="ok")

    monkeypatch.setattr(qt, "run_quick_task", _fake_run_quick_task)

    appended = {}

    class _V:
        def append_chat_history(self, e):
            appended["ts"] = e["ts"]      # the id the Stop button will read
        def update_chat_history_entry(self, ts, f):
            appended["update_ts"] = ts

    v = _V()

    # ── exactly what chat_page._run does ──
    from datetime import datetime as _dt
    chat_ts = _dt.now().isoformat(timespec="seconds")
    cancel_event = reg.register(chat_ts)
    try:
        qt.submit_quick_task("hi", None, v, chat_ts=chat_ts, cancel_event=cancel_event)
        # The appended chat-history entry id MUST equal the canonical registry key.
        assert appended["ts"] == chat_ts
        assert appended.get("update_ts") == chat_ts
        # The Stop button reads entry["ts"] and calls request_cancel(that). It works.
        assert reg.request_cancel(appended["ts"]) is True
        assert cancel_event.is_set() is True
    finally:
        reg.unregister(chat_ts)


def test_direct_lane_stop_button_cancels_via_canonical_chat_ts(monkeypatch):
    """Direct lane: the historical NEVER-matches case. Register under canonical ts,
    run run_direct_task(chat_ts=ts), assert request_cancel(entry ts) is True."""
    import systemu.pipelines.direct_task as dt
    from systemu.runtime import chat_task_registry as reg

    appended = {}

    class _FakeRuntime:
        def __init__(self, config, vault): pass
        async def execute(self, shadow, activity, **kwargs):
            return {"status": "success", "final_summary": "done",
                    "execution_id": "exec_x"}

    class _Activity:
        id = "act_1"; scroll_id = "scr_1"; required_tool_ids: list = []; origin = ""
    class _Scroll:
        id = "scr_1"; intent = "x"
    class _Shadow:
        id = "sh_1"; name = "Generalist"
    activity, scroll, shadow = _Activity(), _Scroll(), _Shadow()

    class _Vault:
        def append_chat_history(self, e): appended["ts"] = e["ts"]
        def update_chat_history_entry(self, ts, f): appended["update_ts"] = ts
        def save_activity(self, a): pass
        def get_tool(self, tid): raise KeyError(tid)

    monkeypatch.setattr("systemu.interface.notifications.set_vault", lambda *a, **k: None)
    monkeypatch.setattr("systemu.pipelines.activity_extractor.init_pipeline", lambda *a, **k: None)
    monkeypatch.setattr("systemu.pipelines.scroll_refiner.refine_from_text", lambda *a, **k: scroll)
    monkeypatch.setattr("systemu.pipelines.activity_extractor.extract_and_process", lambda *a, **k: activity)
    monkeypatch.setattr("systemu.pipelines.shadow_decision.decide_shadow", lambda *a, **k: shadow)
    monkeypatch.setattr("systemu.runtime.shadow_runtime.ShadowRuntime", _FakeRuntime)
    monkeypatch.setattr(dt, "_maybe_trigger_fact_extraction", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(dt, "_maybe_extract_skill_and_consolidate", lambda *a, **k: None, raising=False)
    import asyncio as _asyncio
    monkeypatch.setattr(dt, "_run_coroutine",
                        lambda coro: _asyncio.new_event_loop().run_until_complete(coro))

    from datetime import datetime as _dt
    chat_ts = _dt.now().isoformat(timespec="seconds")
    cancel_event = reg.register(chat_ts)
    try:
        dt.run_direct_task("x", None, _Vault(), chat_ts=chat_ts, cancel_event=cancel_event)
        # The appended chat-history entry id MUST equal the canonical registry key
        # (pre-fix this was utcnow().isoformat() — NEVER equal to chat_ts).
        assert appended["ts"] == chat_ts
        assert appended.get("update_ts") == chat_ts
        # The Stop button reads entry["ts"] and calls request_cancel(that). It works.
        assert reg.request_cancel(appended["ts"]) is True
        assert cancel_event.is_set() is True
    finally:
        reg.unregister(chat_ts)


# ── REGRESSION (review FIX 3) — watchdog skips an operator-cancelling shadow ────

def test_check_stuck_skips_cancelling_entry():
    """An entry the operator cancelled (status='cancelling') is winding down, not
    stuck. The watchdog must not pop+re-submit it even past STUCK_THRESHOLD_S."""
    from systemu.runtime import supervisor as sup_mod
    sup = _bare_supervisor()
    sup._publish = lambda *a, **k: None
    sup.submit = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("watchdog re-submitted a cancelling entry"))
    sup._task_queue = None
    sup._dead_letters = []
    sup._dl_lock = threading.Lock()

    ev = threading.Event()
    # Silent for well over the threshold — would be "stuck" if not for the guard.
    long_ago = time.monotonic() - (sup_mod.STUCK_THRESHOLD_S + 60)
    sup._running["act_x_sub_1"] = {
        "cancel_event": ev,
        "payload": {"activity_id": "act_x", "shadow_id": "sh_1", "origin": "chat"},
        "status": "cancelling",
        "last_heartbeat_at": time.time() - (sup_mod.STUCK_THRESHOLD_S + 60),
        "last_heartbeat_at_mono": long_ago,
    }
    sup._check_stuck_shadows()
    # The cancelling entry must NOT have been popped/re-submitted by the watchdog.
    assert "act_x_sub_1" in sup._running


# ── REGRESSION (review FIX 3A) — chat cancel id is microsecond, not seconds ─────

def test_chat_cancel_id_generation_is_microsecond_not_seconds():
    """3A: the chat cancel-registry id (task_ts in chat_page._run) must be
    microsecond-precise. At second granularity two submissions in the same
    wall-clock second collide (shared cancel token + clobbered history rows).
    Pin the generation line so a refactor can't reintroduce timespec='seconds'."""
    import inspect
    from systemu.interface.pages import chat_page
    gen = [ln for ln in inspect.getsource(chat_page).splitlines()
           if "task_ts = _dt.now()" in ln]
    assert gen, "could not find the task_ts generation line in chat_page"
    assert all("timespec" not in ln for ln in gen), \
        "task_ts must be microsecond-precise (no timespec='seconds'): %r" % gen


def test_same_second_distinct_microsecond_ids_isolate_in_registry():
    """Two submissions within the same wall-clock second (distinct microseconds,
    as the fixed generation produces) get DISTINCT cancel tokens — cancelling one
    must not cancel the other (the exact collision the seconds-precision id caused)."""
    from systemu.runtime import chat_task_registry as reg
    ts1 = "2026-06-15T12:00:00.000111"
    ts2 = "2026-06-15T12:00:00.000222"   # same second, distinct microsecond
    reg.unregister(ts1); reg.unregister(ts2)
    e1 = reg.register(ts1)
    e2 = reg.register(ts2)
    try:
        assert e1 is not e2
        assert reg.request_cancel(ts1) is True
        assert e1.is_set() is True
        assert e2.is_set() is False   # cancelling one leaves the other running
    finally:
        reg.unregister(ts1); reg.unregister(ts2)
