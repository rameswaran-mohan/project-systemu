"""v0.8.16 — live expandable origin-partitioned event panes."""
import pytest


class TestOrigin:
    def test_origins_constant(self):
        from systemu.core.models import ORIGINS
        assert ORIGINS == {"chat", "capture", "manual", "scheduled", "system"}

    def test_coerce_origin_known(self):
        from systemu.core.models import coerce_origin
        assert coerce_origin("chat") == "chat"
        assert coerce_origin("ui-submit") == "chat"
        assert coerce_origin("manual") == "manual"
        assert coerce_origin("scheduled") == "scheduled"
        assert coerce_origin("capture") == "capture"

    def test_coerce_origin_recovery_is_system(self):
        from systemu.core.models import coerce_origin
        for r in ("restart-restore", "crash-recovery", "db-restore", "retry-1",
                  "startup_recovery_assigned"):
            assert coerce_origin(r) == "system"

    def test_coerce_origin_unknown_defaults_manual(self):
        from systemu.core.models import coerce_origin
        assert coerce_origin("") == "manual"
        assert coerce_origin(None) == "manual"
        assert coerce_origin("something_new") == "manual"

    def test_activity_has_origin_default_manual(self):
        from systemu.core.models import Activity, ActivityStatus
        a = Activity(id="a", name="n", scroll_id="s")
        assert a.origin == "manual"


# ─────────────────────────────────────────────────────────────────────────────
#  Task 2 — Supervisor.submit(origin=...) carries origin on the queued event
# ─────────────────────────────────────────────────────────────────────────────

def _bare_supervisor():
    """Minimal Supervisor stub exercising the real `submit` path.

    Sets only the instance attributes `submit` touches (no SQLite queue, no
    affinity loop).  Reuses the `Supervisor.__new__` pattern from
    tests/test_v042a_affinity_routing.py / test_v050e_resume.py.
    """
    import threading
    import queue as _queue
    from systemu.runtime.supervisor import Supervisor

    sup = Supervisor.__new__(Supervisor)
    sup._task_queue = None
    sup._queue = _queue.PriorityQueue()
    sup._running = {}
    sup._running_lock = threading.Lock()
    sup._pending_activity_ids = set()
    sup._pending_lock = threading.Lock()
    sup._aname = lambda aid: aid  # name resolver stub
    return sup


class TestSubmitOrigin:
    @pytest.fixture
    def captured(self):
        """Subscribe to the real EventBus and capture every published event."""
        from systemu.interface.event_bus import EventBus
        events = []
        unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
        try:
            yield events
        finally:
            unsub()

    def test_default_reason_manual_origin(self, captured):
        sup = _bare_supervisor()
        sup.submit("act-1", "sh-1", consult_affinity_log=False)  # reason defaults to "manual"
        queued = [e for e in captured if "queued" in e.get("message", "").lower()]
        assert queued, "no queued event published"
        assert queued[-1]["origin"] == "manual"

    def test_reason_chat_maps_to_chat_origin(self, captured):
        sup = _bare_supervisor()
        sup.submit("act-2", "sh-1", reason="chat", consult_affinity_log=False)
        queued = [e for e in captured if "queued" in e.get("message", "").lower()]
        assert queued[-1]["origin"] == "chat"

    def test_explicit_origin_overrides_reason(self, captured):
        sup = _bare_supervisor()
        sup.submit("act-3", "sh-1", reason="manual", origin="capture",
                   consult_affinity_log=False)
        queued = [e for e in captured if "queued" in e.get("message", "").lower()]
        assert queued[-1]["origin"] == "capture"

    def test_origin_stored_on_queue_payload(self):
        sup = _bare_supervisor()
        sup.submit("act-4", "sh-1", reason="chat", consult_affinity_log=False)
        _prio, _ts, payload = sup._queue.get_nowait()
        assert payload["origin"] == "chat"


# ─────────────────────────────────────────────────────────────────────────────
#  Task 3 — ShadowRuntime stamps origin on every published event
# ─────────────────────────────────────────────────────────────────────────────

class TestRuntimeStampsOrigin:
    def test_stamp_adds_origin(self):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime)
        rt._origin = "chat"
        ev = rt._stamp({"message": "x"})
        assert ev["origin"] == "chat"

    def test_stamp_defaults_when_unset(self):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime)
        ev = rt._stamp({"message": "x"})   # no _origin set
        assert ev["origin"] in ("manual", "system")  # safe default

    def test_stamp_does_not_override_existing_origin(self):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime)
        rt._origin = "chat"
        ev = rt._stamp({"message": "x", "origin": "capture"})
        assert ev["origin"] == "capture"  # setdefault — does not clobber

    def test_execute_accepts_origin_kwarg(self):
        import inspect
        from systemu.runtime.shadow_runtime import ShadowRuntime
        sig = inspect.signature(ShadowRuntime.execute)
        assert "origin" in sig.parameters


class TestRecalibrationCardOrigin:
    def test_publish_recalibration_card_carries_origin(self, monkeypatch):
        # 4th publish path (reached via RECALIBRATE_TOOL during execution) must
        # also carry origin, or a chat recalibration card mis-partitions.
        from types import SimpleNamespace
        import systemu.pipelines.tool_recalibrator as tr
        from systemu.interface.event_bus import EventBus
        captured = []
        monkeypatch.setattr(EventBus, "get",
                            classmethod(lambda cls: SimpleNamespace(publish=lambda e: captured.append(e))))
        monkeypatch.setattr(tr, "_compose_approval_message", lambda r: "msg")
        result = SimpleNamespace(success=True, mode="bump", forced_fallback=False,
                                 original_tool_id="tool_x", new_tool_id="tool_x",
                                 to_card_context=lambda: {})
        tr.publish_recalibration_card(result=result, shadow_id="sh", execution_id="ex",
                                      scroll_id=None, origin="chat")
        assert captured and captured[0]["origin"] == "chat"

    def test_recalibrate_tool_directive_accepts_origin(self):
        import inspect
        from systemu.runtime.shadow_runtime import _apply_recalibrate_tool_directive
        assert "origin" in inspect.signature(_apply_recalibrate_tool_directive).parameters


# ─────────────────────────────────────────────────────────────────────────────
#  Task 4 — Entry points set the right trigger origin
# ─────────────────────────────────────────────────────────────────────────────

class TestEntryOrigins:
    def test_direct_task_sets_chat_origin(self):
        import inspect
        from systemu.pipelines import direct_task
        src = inspect.getsource(direct_task)
        assert 'origin="chat"' in src or "origin='chat'" in src

    def test_decide_shadow_passes_activity_origin(self):
        import inspect
        from systemu.pipelines import shadow_decision
        src = inspect.getsource(shadow_decision)
        # origin flows from the activity, not the awaken reason
        assert "origin=" in src and "shadow_awakened" in src
        assert 'getattr(activity, "origin"' in src or "getattr(activity, 'origin'" in src

    def test_capture_activity_sets_capture_origin(self):
        # The capture activity is created in activity_extractor.extract_and_process
        # (the shared creator). Origin is derived from the scroll source there:
        # a capture scroll → "capture", a chat scroll → "chat".
        import inspect
        from systemu.pipelines import activity_extractor
        src = inspect.getsource(activity_extractor)
        assert '"capture"' in src and "origin" in src

    def test_scheduler_recovery_submit_passes_origin(self):
        import inspect
        from systemu.scheduler import jobs
        src = inspect.getsource(jobs)
        assert "origin=" in src

    def test_operator_submit_passes_manual_origin(self):
        import inspect
        from systemu.interface.pages import systemu_chat
        src = inspect.getsource(systemu_chat)
        assert 'origin="manual"' in src or "origin='manual'" in src


# ─────────────────────────────────────────────────────────────────────────────
#  Task 5 — Live pane: deque + UI-timer + origin filter + system toggle
# ─────────────────────────────────────────────────────────────────────────────

class TestPaneFilter:
    def test_passes_origin_filter(self):
        from systemu.interface.components.live_events_pane import _passes_origin_filter
        # chat pane: show only chat (+system when toggled)
        assert _passes_origin_filter({"origin": "chat"}, {"chat"}, show_system=False) is True
        assert _passes_origin_filter({"origin": "manual"}, {"chat"}, show_system=False) is False
        assert _passes_origin_filter({"origin": "system"}, {"chat"}, show_system=False) is False
        assert _passes_origin_filter({"origin": "system"}, {"chat"}, show_system=True) is True
        # missing origin → treated as manual
        assert _passes_origin_filter({}, {"manual"}, show_system=False) is True

    def test_passes_origin_filter_manual_pane(self):
        from systemu.interface.components.live_events_pane import _passes_origin_filter
        origins = {"capture", "manual", "scheduled"}
        assert _passes_origin_filter({"origin": "capture"}, origins, show_system=False) is True
        assert _passes_origin_filter({"origin": "manual"}, origins, show_system=False) is True
        assert _passes_origin_filter({"origin": "scheduled"}, origins, show_system=False) is True
        assert _passes_origin_filter({"origin": "chat"}, origins, show_system=False) is False
        # system stays hidden until toggled, regardless of which pane
        assert _passes_origin_filter({"origin": "system"}, origins, show_system=False) is False
        assert _passes_origin_filter({"origin": "system"}, origins, show_system=True) is True


# ─────────────────────────────────────────────────────────────────────────────
#  Task 6 — Manual Logs rename + origin-filtered live console panes
# ─────────────────────────────────────────────────────────────────────────────

class TestManualLogsRename:
    def test_console_uses_origin_filtered_panes(self):
        import inspect
        from systemu.interface.pages import console
        src = inspect.getsource(console)
        assert "Manual Logs" in src
        assert "origins=" in src and '"chat"' in src

    def test_console_manual_pane_origins(self):
        import inspect
        from systemu.interface.pages import console
        src = inspect.getsource(console)
        # Manual Logs pane fans the non-chat origins
        for o in ("capture", "manual", "scheduled"):
            assert f'"{o}"' in src

    def test_insights_tab_renamed(self):
        import inspect
        from systemu.interface.pages import insights
        assert "Manual Logs" in inspect.getsource(insights)

    def test_insights_events_route_preserved(self):
        # The /insights?tab=events route must keep working (only labels change).
        import inspect
        from systemu.interface.pages import insights
        src = inspect.getsource(insights)
        assert '"events"' in src or "'events'" in src

    def test_notifications_renamed(self):
        import inspect
        from systemu.interface.pages import notifications_page
        assert "Manual Logs" in inspect.getsource(notifications_page)


# ─────────────────────────────────────────────────────────────────────────────
#  Task 7 — Per-iteration detail events (reasoning + tool I/O)
# ─────────────────────────────────────────────────────────────────────────────

class TestIterationEvent:
    def test_builds_bounded_detail_event(self):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime); rt._origin = "chat"
        ev = rt._iteration_event(
            iteration=1,
            decision={"action": "TOOL_CALL", "tool_name": "web_navigate",
                      "parameters": {"url": "x"}, "reasoning": "go"},
            tool_result={"success": True}, execution_id="exec_1",
            llm_ref={"exec_id": "exec_1", "call_index": 0},
        )
        assert ev["origin"] == "chat"
        assert ev["details"]["tool_name"] == "web_navigate"
        assert ev["details"]["reasoning"] == "go"
        assert ev["details"]["llm_ref"] == {"exec_id": "exec_1", "call_index": 0}
        assert "iter=1" in ev["message"]

    def test_tool_result_truncated(self):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime); rt._origin = "manual"
        big = "x" * 10000
        ev = rt._iteration_event(
            iteration=2,
            decision={"action": "TOOL_CALL", "tool_name": "t"},
            tool_result=big, execution_id="exec_2",
        )
        # bounded ≤ 4000 chars
        assert ev["details"]["tool_result"] is not None
        assert len(ev["details"]["tool_result"]) <= 4000

    def test_no_tool_result_is_none(self):
        from systemu.runtime.shadow_runtime import ShadowRuntime
        rt = ShadowRuntime.__new__(ShadowRuntime); rt._origin = "manual"
        ev = rt._iteration_event(
            iteration=3,
            decision={"action": "THINK", "thought": "hmm"},
            execution_id="exec_3",
        )
        assert ev["details"]["tool_result"] is None
        # THINK has no tool_name → message is just "iter=3 THINK"
        assert ev["message"].startswith("iter=3")
        # reasoning falls back to "thought" when "reasoning" absent
        assert ev["details"]["reasoning"] == "hmm"


# ─────────────────────────────────────────────────────────────────────────────
#  Task 8 — Per-execution LLM transcript writer + llm_ref
# ─────────────────────────────────────────────────────────────────────────────

class TestLLMTranscript:
    def test_append_and_read(self, tmp_path):
        from systemu.runtime.llm_transcript import append_call, read_call
        idx = append_call(tmp_path, "exec_1", {"system": "sys", "user": "u", "response": "r1"})
        assert idx == 0
        idx2 = append_call(tmp_path, "exec_1", {"response": "r2"})
        assert idx2 == 1
        assert read_call(tmp_path, "exec_1", 0)["response"] == "r1"
        assert read_call(tmp_path, "exec_1", 1)["response"] == "r2"
        assert read_call(tmp_path, "exec_1", 9) is None  # out of range → None

    def test_read_missing_execution_is_none(self, tmp_path):
        from systemu.runtime.llm_transcript import read_call
        assert read_call(tmp_path, "nope", 0) is None

    def test_entry_string_fields_truncated(self, tmp_path):
        from systemu.runtime.llm_transcript import append_call, read_call
        big = "y" * 50000
        idx = append_call(tmp_path, "exec_x", {"response": big, "n": 7})
        assert idx == 0
        entry = read_call(tmp_path, "exec_x", 0)
        assert len(entry["response"]) <= 20000
        assert entry["n"] == 7  # non-string fields untouched

    def test_writes_to_executions_subdir(self, tmp_path):
        from systemu.runtime.llm_transcript import append_call
        append_call(tmp_path, "exec_p", {"response": "ok"})
        assert (tmp_path / "executions" / "exec_p" / "llm_transcript.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
#  Task 9 — Expandable event rows + lazy raw-LLM fetch
# ─────────────────────────────────────────────────────────────────────────────

class TestDetailRender:
    def test_has_details_true_with_nonempty(self):
        from systemu.interface.components.live_events_pane import _has_details
        assert _has_details({"details": {"reasoning": "x"}}) is True

    def test_has_details_false_empty(self):
        from systemu.interface.components.live_events_pane import _has_details
        assert _has_details({"details": {}}) is False

    def test_has_details_false_absent(self):
        from systemu.interface.components.live_events_pane import _has_details
        assert _has_details({"message": "plain"}) is False

    def test_has_details_false_all_none_values(self):
        from systemu.interface.components.live_events_pane import _has_details
        # details present but every value is falsy → no real detail to show
        assert _has_details({"details": {"reasoning": None, "tool_result": None}}) is False

    def test_has_details_true_when_any_value_present(self):
        from systemu.interface.components.live_events_pane import _has_details
        assert _has_details({"details": {"reasoning": None, "action": "THINK"}}) is True

    def test_lazy_llm_uses_ref(self, tmp_path):
        from systemu.interface.components import live_events_pane as p
        from systemu.runtime.llm_transcript import append_call
        append_call(tmp_path, "exec_1", {"response": "hello"})
        got = p._load_llm_text(tmp_path, {"exec_id": "exec_1", "call_index": 0})
        assert "hello" in got

    def test_lazy_llm_none_ref_is_safe(self, tmp_path):
        from systemu.interface.components import live_events_pane as p
        got = p._load_llm_text(tmp_path, None)
        assert isinstance(got, str)  # safe string, no raise

    def test_lazy_llm_missing_entry_is_safe(self, tmp_path):
        from systemu.interface.components import live_events_pane as p
        got = p._load_llm_text(tmp_path, {"exec_id": "nope", "call_index": 0})
        assert isinstance(got, str)  # no transcript → safe string, no raise

    def test_lazy_llm_missing_vault_root_is_safe(self):
        from systemu.interface.components import live_events_pane as p
        got = p._load_llm_text(None, {"exec_id": "exec_1", "call_index": 0})
        assert isinstance(got, str)  # None vault root → safe string, no raise


# ─────────────────────────────────────────────────────────────────────────────
#  Integration gaps (final-review follow-ups) — TOP-LEVEL `origin` on every
#  execution-reachable published event.
# ─────────────────────────────────────────────────────────────────────────────

class TestLogEventLiftsOrigin:
    """Fix #1 — log_event surfaces context['origin'] to a TOP-LEVEL key.

    The live pane partitions on ``event["origin"]`` (top-level), but the
    runtime publishes via ``log_event(..., context={"origin": ...})``.  Without
    lifting it, every chat-run shadow event mis-partitions into Manual Logs.
    """

    @pytest.fixture
    def captured(self):
        """Subscribe to the EventBus and capture every published event."""
        from systemu.interface.event_bus import EventBus
        events = []
        unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
        try:
            yield events
        finally:
            unsub()

    def test_published_event_has_top_level_origin_from_context(self, captured):
        from systemu.interface import notifications
        notifications.log_event("INFO", "shadow", "hello", context={"origin": "chat"})
        assert captured, "log_event published nothing to the EventBus"
        assert captured[-1].get("origin") == "chat"

    def test_published_event_defaults_origin_manual(self, captured):
        from systemu.interface import notifications
        notifications.log_event("INFO", "system", "boot")  # no context
        assert captured[-1].get("origin") == "manual"

    def test_persisted_event_has_top_level_origin(self, tmp_path, monkeypatch):
        """The jsonl entry written to event_log.jsonl also carries origin."""
        import json
        from systemu.interface import notifications
        log_path = tmp_path / "event_log.jsonl"
        monkeypatch.setattr(notifications, "_event_log_path", log_path)
        notifications.log_event("INFO", "shadow", "persist me",
                                context={"origin": "scheduled"})
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines, "nothing persisted to event_log.jsonl"
        entry = json.loads(lines[-1])
        assert entry["origin"] == "scheduled"

    def test_passes_origin_filter_after_lift(self, captured):
        """End-to-end: a lifted chat event lands in the chat pane, not Manual Logs."""
        from systemu.interface import notifications
        from systemu.interface.components.live_events_pane import _passes_origin_filter
        notifications.log_event("INFO", "shadow", "iter", context={"origin": "chat"})
        ev = captured[-1]
        assert _passes_origin_filter(ev, {"chat"}, show_system=False) is True
        assert _passes_origin_filter(
            ev, {"capture", "manual", "scheduled"}, show_system=False
        ) is False


class TestSupervisorLifecycleOrigin:
    """Fix #2 — supervisor lifecycle _publish calls carry top-level origin.

    The queued ▶️/✅/🔄/💀 events partition the live panes; without origin they
    mis-route a chat run into Manual Logs.  The queue payload carries
    ``payload["origin"]`` — the lifecycle publishes must thread it.
    """

    @pytest.fixture
    def captured(self):
        from systemu.interface.event_bus import EventBus
        events = []
        unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
        try:
            yield events
        finally:
            unsub()

    def test_executing_event_carries_origin(self, captured):
        sup = _bare_supervisor()
        payload = {"activity_id": "act-x", "shadow_id": "sh-1",
                   "submission_id": "sub-1", "retry_count": 0, "origin": "chat"}
        # Drive only the lifecycle publish portion of _run_shadow_guarded by
        # invoking the "▶️ Executing" _publish exactly as the runtime does.
        sup._update_heartbeat = lambda *a, **k: None
        sup._publish(
            f"▶️ Executing: {sup._aname(payload['activity_id'])} (retry=0)",
            context={"activity_id": payload["activity_id"]},
            origin=payload.get("origin"),
        )
        execu = [e for e in captured if "executing" in e.get("message", "").lower()]
        assert execu and execu[-1].get("origin") == "chat"

    def test_completed_event_carries_origin(self, captured):
        sup = _bare_supervisor()
        payload = {"activity_id": "act-y", "shadow_id": "sh-1",
                   "submission_id": "sub-2", "origin": "scheduled"}
        sup._handle_result(payload, {"status": "success"})
        done = [e for e in captured if "completed" in e.get("message", "").lower()]
        assert done and done[-1].get("origin") == "scheduled"

    def test_dead_letter_event_carries_origin(self, captured, monkeypatch):
        import threading as _t
        sup = _bare_supervisor()
        # Dead-letter path appends to _dead_letters under _dl_lock and spins a
        # diagnosis thread — stub both so we exercise only the publish.
        sup._dl_lock = _t.Lock()
        sup._dead_letters = []
        monkeypatch.setattr(sup, "_analyze_failure", lambda *a, **k: None)
        payload = {"activity_id": "act-z", "shadow_id": "sh-1",
                   "submission_id": "sub-3", "origin": "capture",
                   "retry_count": 99}  # retry_count >= MAX_RETRIES → dead-letter
        sup._handle_result(payload, {"status": "failure", "error": "boom"})
        dl = [e for e in captured if "dead-letter" in e.get("message", "").lower()]
        assert dl and dl[-1].get("origin") == "capture"

    def test_source_lifecycle_publishes_thread_origin(self):
        """Source-level guard: the lifecycle _publish sites pass origin=."""
        import inspect
        from systemu.runtime import supervisor
        src = inspect.getsource(supervisor)
        # Each lifecycle marker must be near an origin= thread; cheap proxy:
        # count origin= occurrences on _publish calls (submit + 4 lifecycle).
        assert src.count("origin=payload") + src.count("origin=resolved_origin") >= 4


class TestStrategyStreamOrigin:
    """Fix #3 — publish_supervisor_action emits a top-level origin, fed from the
    runtime's _origin via ExecutionMind construction."""

    def test_execution_mind_accepts_origin(self):
        import inspect
        from systemu.runtime.execution_mind import ExecutionMind
        assert "origin" in inspect.signature(ExecutionMind.__init__).parameters

    def test_publish_supervisor_action_accepts_origin(self):
        import inspect
        from systemu.interface.event_bus import EventBus
        assert "origin" in inspect.signature(EventBus.publish_supervisor_action).parameters

    def test_publish_supervisor_action_stamps_top_level_origin(self, monkeypatch):
        from systemu.interface.event_bus import EventBus
        captured = []
        monkeypatch.setattr(EventBus, "publish",
                            lambda self, e: captured.append(e))
        EventBus.get().publish_supervisor_action(
            execution_id="ex-1", action="NUDGE", origin="chat",
        )
        assert captured and captured[-1].get("origin") == "chat"

    def test_publish_supervisor_action_defaults_origin(self, monkeypatch):
        from systemu.interface.event_bus import EventBus
        captured = []
        monkeypatch.setattr(EventBus, "publish",
                            lambda self, e: captured.append(e))
        EventBus.get().publish_supervisor_action(execution_id="ex-2", action="DO_NOTHING")
        # default must be a safe origin string (not missing) so it never
        # mis-partitions into a pane it doesn't belong to.
        assert captured[-1].get("origin") in ("system", "manual", "chat")

    def test_shadow_runtime_constructs_mind_with_origin(self):
        import inspect
        from systemu.runtime import shadow_runtime
        src = inspect.getsource(shadow_runtime)
        # ExecutionMind(...) construction threads the runtime origin.
        assert "origin=self._origin" in src


class TestInsightsManualLogsLive:
    """Fix #4 — the full-page Manual Logs (Events) tab renders the live,
    origin-filtered Manual Logs pane (additive — pending content preserved)."""

    def test_events_tab_renders_live_pane(self):
        import inspect
        from systemu.interface.pages import insights
        src = inspect.getsource(insights)
        assert "build_supervisor_events_pane" in src

    def test_events_tab_uses_manual_log_origins(self):
        import inspect
        from systemu.interface.pages import insights
        src = inspect.getsource(insights)
        for o in ("capture", "manual", "scheduled"):
            assert f'"{o}"' in src

    def test_events_tab_keeps_pending_notifications(self):
        import inspect
        from systemu.interface.pages import insights
        src = inspect.getsource(insights)
        # The existing file-tail notifications content must still be present.
        assert "build_notifications_page" in src
