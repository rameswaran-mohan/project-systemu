"""D6 (dogfood 0.10.28) - a wedged RUNNING chat task must not block in silence.

Operator witness, with a task parked RUNNING on a gate:
  * new task + Enter          -> silently ignored
  * /continue                 -> silently ignored
  * the stop button           -> no visible acknowledgment, state unchanged
  * daemon restart            -> the activity came back as RUNNING (persistence
                                 is correct) with no operator path from Chat to
                                 clear it.

Ruling under test:
  (a) every refused submission gets a toast saying WHY and what to do;
  (b) the stop button CANCELS through the runtime cancel entry - including a
      task parked on a gate - and the UI says "Stopped <title>" or exactly why
      it could not;
  (c) /continue on a parked task explains what it is waiting on and where to
      resolve it;
  (d) a restored RUNNING row with no live worker is rendered as such.

Handler seams only: a fake chat-history store, a fake supervisor, a fake
decision queue. No vault on disk, no LLM, no daemon.
"""
from __future__ import annotations

import inspect
import threading

import pytest


# ---------------------------------------------------------------- fakes ------

class _FakeVault:
    """Chat-history store stand-in: load / update round-trip only."""

    def __init__(self, entries=None):
        self.entries = list(entries or [])
        self.updates = []
        self.raise_on_update = None

    def load_chat_history(self, limit=50):
        return list(self.entries[-limit:])

    def update_chat_history_entry(self, ts, updates):
        if self.raise_on_update is not None:
            raise self.raise_on_update
        self.updates.append((ts, dict(updates)))
        for e in self.entries:
            if e.get("ts") == ts:
                e.update(updates)


class _FakeSupervisor:
    def __init__(self, running_activity_ids=()):
        self.cancelled = []
        self._running = {
            f"key_{i}": {"payload": {"activity_id": aid}}
            for i, aid in enumerate(running_activity_ids)
        }
        self._running_lock = threading.Lock()
        self._pending_activity_ids = set()
        self._pending_lock = threading.Lock()

    def request_cancel_by_activity(self, activity_id):
        self.cancelled.append(activity_id)
        return activity_id in {
            e["payload"]["activity_id"] for e in self._running.values()
        }


class _FakeQueue:
    def __init__(self, expires=True):
        self.expired = []
        self._expires = expires

    def expire_by_dedup_key(self, dedup_key):
        self.expired.append(dedup_key)
        return self._expires


def _running_entry(**over):
    entry = {
        "ts": "2026-09-07T09:15:30.100000",
        "prompt": "collect the weekly numbers and write them up",
        "status": "running",
        "activity_id": "act_wedged",
    }
    entry.update(over)
    return entry


def _parked_entry(**over):
    return _running_entry(
        status="pending_decision",
        decision_id="dec_abc",
        dedup_key="cmd_gate:rm",
        **over,
    )


# ------------------------------------------------ (a) refusal names the WHY ---

class TestRefusalExplainsItself:
    def test_a_running_task_is_found(self):
        from systemu.interface.pages.chat_page import find_active_chat_task
        entries = [{"ts": "old", "status": "success"}, _running_entry()]
        found = find_active_chat_task(entries)
        assert found is not None and found["ts"] == _running_entry()["ts"]

    def test_queued_counts_as_active(self):
        from systemu.interface.pages.chat_page import find_active_chat_task
        assert find_active_chat_task([_running_entry(status="queued")]) is not None

    def test_only_terminal_rows_means_no_blocker(self):
        from systemu.interface.pages.chat_page import find_active_chat_task
        entries = [{"ts": "a", "status": "success"},
                   {"ts": "b", "status": "cancelled"},
                   {"ts": "c", "status": "failed"}]
        assert find_active_chat_task(entries) is None

    def test_newest_active_row_wins(self):
        from systemu.interface.pages.chat_page import find_active_chat_task
        entries = [_running_entry(ts="older"), _running_entry(ts="newer")]
        assert find_active_chat_task(entries)["ts"] == "newer"

    def test_live_task_toast_names_it_and_says_what_to_do(self):
        from systemu.interface.pages.chat_page import active_task_toast
        msg = active_task_toast(_running_entry(), live=True)
        assert "A task is running" in msg
        assert "collect the weekly numbers" in msg   # the <title>
        assert "2026-09-07 09:15:30" in msg          # the <time>
        assert "Stop it or wait." in msg
        msg.encode("ascii")

    def test_restored_task_toast_says_nothing_is_working_on_it(self):
        from systemu.interface.pages.chat_page import active_task_toast
        msg = active_task_toast(_running_entry(), live=False)
        assert "restored" in msg.lower()
        assert "Stop it" in msg
        msg.encode("ascii")

    def test_refused_submit_does_not_start_and_does_not_clear(self):
        """D6(a) x D5: refusal is a failure - the text stays in the composer."""
        from systemu.interface.pages.chat_page import handle_chat_submit
        calls, notices = [], []
        out = handle_chat_submit(
            text="a brand new task",
            entries=[_running_entry()],
            start=lambda: calls.append("start") or "ts",
            await_ack=lambda tid: (True, ""),
            notify=lambda m, **k: notices.append((m, k.get("type"))),
            clear=lambda: calls.append("clear"),
        )
        assert out is None
        assert calls == [], "a refused submit must neither start work nor clear the text"
        assert notices, "silence is the defect"
        assert "A task is running" in notices[-1][0]

    def test_refusal_toast_is_not_silently_dropped(self):
        """MUTATION PIN (D6a): delete the notify() in the refusal branch -> red."""
        from systemu.interface.pages.chat_page import handle_chat_submit
        notices = []
        handle_chat_submit(
            text="another one", entries=[_running_entry()],
            start=lambda: "ts", await_ack=lambda tid: (True, ""),
            notify=lambda m, **k: notices.append(m), clear=lambda: None,
        )
        assert len(notices) == 1


# -------------------------------------------------- (c) /continue explains ----

class TestContinueExplainsTheWait:
    def test_continue_on_a_gate_parked_task_names_the_gate(self):
        from systemu.interface.pages.chat_page import continue_waiting_toast
        msg = continue_waiting_toast(_parked_entry())
        assert "waiting" in msg.lower()
        assert "Notifications" in msg or "Inbox" in msg   # where to resolve it
        msg.encode("ascii")

    def test_continue_on_a_tools_parked_task_names_the_tools(self):
        from systemu.interface.pages.chat_page import continue_waiting_toast
        msg = continue_waiting_toast(
            _running_entry(status="waiting_on_tools", missing_tools=["pdf_split"]))
        assert "pdf_split" in msg

    def test_continue_on_a_plain_running_task_says_so(self):
        from systemu.interface.pages.chat_page import continue_waiting_toast
        msg = continue_waiting_toast(_running_entry())
        assert "running" in msg.lower()
        assert "Stop" in msg or "stop" in msg

    def test_continue_is_refused_with_the_waiting_message_not_the_generic_one(self):
        from systemu.interface.pages.chat_page import handle_chat_submit
        notices = []
        out = handle_chat_submit(
            text="/continue and also email it",
            entries=[_parked_entry()],
            start=lambda: "ts", await_ack=lambda tid: (True, ""),
            notify=lambda m, **k: notices.append(m), clear=lambda: None,
        )
        assert out is None
        assert "waiting" in notices[-1].lower(), (
            "/continue on a parked task must explain the wait, not repeat the "
            "generic 'a task is running' line"
        )


# ------------------------------------------------------ (d) restored marker ---

class TestRestoredWithoutAWorker:
    def test_registered_chat_task_is_live(self):
        from systemu.runtime import chat_task_registry as reg
        ts = "2026-09-07T09:15:30.100000"
        reg.unregister(ts)
        reg.register(ts)
        try:
            assert reg.chat_task_is_live(_running_entry(ts=ts)) is True
        finally:
            reg.unregister(ts)

    def test_row_with_no_token_and_no_supervisor_work_is_not_live(self):
        from systemu.runtime import chat_task_registry as reg
        reg.unregister(_running_entry()["ts"])
        assert reg.chat_task_is_live(_running_entry()) is False

    def test_supervisor_running_the_activity_counts_as_live(self):
        from systemu.runtime import chat_task_registry as reg
        reg.unregister(_running_entry()["ts"])
        sup = _FakeSupervisor(running_activity_ids=["act_wedged"])
        assert reg.chat_task_is_live(_running_entry(), supervisor=sup) is True

    def test_supervisor_pending_counts_as_live(self):
        from systemu.runtime import chat_task_registry as reg
        reg.unregister(_running_entry()["ts"])
        sup = _FakeSupervisor()
        sup._pending_activity_ids.add("act_wedged")
        assert reg.chat_task_is_live(_running_entry(), supervisor=sup) is True

    def test_terminal_row_is_never_live(self):
        from systemu.runtime import chat_task_registry as reg
        assert reg.chat_task_is_live(_running_entry(status="success")) is False

    def test_restored_note_is_shown_only_when_nothing_works_it(self):
        from systemu.interface.pages.chat_page import restored_task_note
        note = restored_task_note(_running_entry(), live=False)
        assert "restored" in note.lower()
        assert "not being worked" in note.lower()
        note.encode("ascii")
        assert restored_task_note(_running_entry(), live=True) == ""
        assert restored_task_note(_running_entry(status="success"), live=False) == ""

    def test_the_card_renders_the_restored_note(self):
        from systemu.interface.pages import chat_page
        src = inspect.getsource(chat_page.build_chat_page)
        assert "restored_task_note(" in src, (
            "a restored RUNNING row still renders as a normal running one"
        )


# ------------------------------------------------------------- (b) the stop ---

class TestStopCancelsForReal:
    def test_stop_signals_the_runtime_and_persists_cancelled(self):
        from systemu.runtime.chat_task_registry import cancel_chat_task
        entry = _running_entry()
        vault = _FakeVault([entry])
        sup = _FakeSupervisor(running_activity_ids=["act_wedged"])

        report = cancel_chat_task(vault, entry["ts"], supervisor=sup)

        assert report["stopped"] is True
        assert sup.cancelled == ["act_wedged"], (
            "the runtime cancel entry must be called with the activity id"
        )
        assert vault.updates, "MUTATION PIN (D6b): a no-op stop leaves the row RUNNING"
        ts_written, payload = vault.updates[-1]
        assert ts_written == entry["ts"]
        assert payload["status"] == "cancelled"

    def test_stop_report_names_the_task(self):
        from systemu.runtime.chat_task_registry import cancel_chat_task
        entry = _running_entry()
        report = cancel_chat_task(_FakeVault([entry]), entry["ts"])
        assert "collect the weekly numbers" in report["title"]

    def test_stop_sets_the_in_process_cancel_token_when_one_is_live(self):
        from systemu.runtime import chat_task_registry as reg
        entry = _running_entry()
        reg.unregister(entry["ts"])
        ev = reg.register(entry["ts"])
        try:
            report = reg.cancel_chat_task(_FakeVault([entry]), entry["ts"])
            assert ev.is_set() is True
            assert report["signalled"] is True
        finally:
            reg.unregister(entry["ts"])

    def test_stop_on_a_gate_parked_task_withdraws_the_card(self):
        from systemu.runtime.chat_task_registry import cancel_chat_task
        entry = _parked_entry()
        queue = _FakeQueue(expires=True)
        report = cancel_chat_task(_FakeVault([entry]), entry["ts"], queue=queue)
        assert queue.expired == ["cmd_gate:rm"]
        assert report["gate_withdrawn"] is True
        assert report["stopped"] is True

    def test_gate_that_cannot_be_withdrawn_leaves_a_note(self):
        from systemu.runtime.chat_task_registry import cancel_chat_task
        entry = _parked_entry()
        queue = _FakeQueue(expires=False)
        report = cancel_chat_task(_FakeVault([entry]), entry["ts"], queue=queue)
        assert report["gate_withdrawn"] is False
        assert "cancelled" in report["gate_note"].lower()
        report["gate_note"].encode("ascii")

    def test_stop_on_an_already_finished_task_says_exactly_why(self):
        from systemu.runtime.chat_task_registry import cancel_chat_task
        entry = _running_entry(status="success")
        vault = _FakeVault([entry])
        report = cancel_chat_task(vault, entry["ts"])
        assert report["stopped"] is False
        assert "success" in report["reason"]
        assert vault.updates == [], "a finished task must not be rewritten"

    def test_stop_on_an_unknown_id_says_exactly_why(self):
        from systemu.runtime.chat_task_registry import cancel_chat_task
        report = cancel_chat_task(_FakeVault([]), "nope")
        assert report["stopped"] is False
        assert report["reason"]
        report["reason"].encode("ascii")

    def test_a_failed_persist_is_reported_not_swallowed(self):
        from systemu.runtime.chat_task_registry import cancel_chat_task
        entry = _running_entry()
        vault = _FakeVault([entry])
        vault.raise_on_update = OSError("disk full")
        report = cancel_chat_task(vault, entry["ts"])
        assert report["stopped"] is False
        assert "disk full" in report["reason"]

    def test_stopped_toast_names_the_task(self):
        from systemu.interface.pages.chat_page import stop_result_message
        msg, kind = stop_result_message(
            {"stopped": True, "title": "collect the weekly numbers",
             "live": True, "gate_note": "", "reason": ""})
        assert msg.startswith("Stopped collect the weekly numbers")
        assert kind == "positive"
        msg.encode("ascii")

    def test_could_not_stop_toast_says_exactly_why(self):
        from systemu.interface.pages.chat_page import stop_result_message
        msg, kind = stop_result_message(
            {"stopped": False, "title": "x", "live": False,
             "gate_note": "", "reason": "the task already finished (success)"})
        assert "the task already finished (success)" in msg
        assert kind in ("warning", "negative")
        msg.encode("ascii")

    def test_stop_toast_carries_the_gate_note(self):
        from systemu.interface.pages.chat_page import stop_result_message
        msg, _ = stop_result_message(
            {"stopped": True, "title": "x", "live": False,
             "gate_note": "task was cancelled", "reason": ""})
        assert "task was cancelled" in msg


# ------------------------------------------------------- reachability pins ---

class TestTheFixIsWired:
    def test_the_stop_button_calls_the_runtime_cancel_entry(self):
        from systemu.interface.pages import chat_page
        src = inspect.getsource(chat_page._make_chat_stop_handler)
        assert "cancel_chat_task(" in src, (
            "the Stop button still only sets an in-process flag"
        )
        assert "stop_result_message(" in src, "the stop result is not surfaced"

    def test_the_submit_handler_looks_for_a_blocking_task(self):
        from systemu.interface.pages import chat_page
        seam = inspect.getsource(chat_page.handle_chat_submit)
        assert "find_active_chat_task(" in seam, (
            "the submit chokepoint no longer checks for an unfinished task"
        )
        page = inspect.getsource(chat_page.build_chat_page)
        assert "handle_chat_submit(" in page, (
            "the chokepoint has no production call site - D6(a) would be inert"
        )

    def test_cancel_entry_lives_in_the_module_that_owns_chat_task_state(self):
        from systemu.runtime import chat_task_registry as reg
        assert callable(getattr(reg, "cancel_chat_task", None))
        assert callable(getattr(reg, "chat_task_is_live", None))
