"""R-UX2 — the quick lane runs OFF the event loop (premise correction for UX-9(c)).

SPEC §15-UX UX-9 names ``_poll_command_choice`` (``pipelines/quick_task.py``) as a
**confirmed** event-loop blocker, and the plan's R-UX2 file list schedules
"``_poll_command_choice`` → EventBus await" on that basis.

Grounding the code rather than the plan: it is **not** an event-loop blocker.
Both production entry points into the quick lane dispatch it onto a daemon
worker thread before any gate poll can run —

  * ``interface/pages/chat_page.py``  — ``threading.Thread(target=_run, daemon=True)``
  * ``pipelines/direct_task.submit_chat_task`` — ``threading.Thread(..., daemon=True)``
    (the shared path for the Telegram ``/chat`` handler and the HTTP task API)

— so its ``time.sleep(1.0)`` block-poll blocks a worker thread, which is what
``_ask_operator_inline``'s own docstring already says ("the chat lane runs on a
daemon thread that simply blocks, so the ReAct loop's state is preserved on the
stack"). The ``tests/conftest.py`` autouse fixture that bounds the poll exists to
stop a 300s stall **per test**, which is a suite-runtime cost, not a UI freeze.

Converting a working, fail-closed gate to an EventBus await on that premise
would rewrite a rail five test modules depend on and buy no loop responsiveness.
What actually protects the loop is the thread affinity itself — so THAT is what
this module pins. If a future change ever dispatches the quick lane inline, the
block-poll really would freeze the dashboard, and this fails first.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from systemu.pipelines import direct_task, quick_task


class _RecordingVault:
    """Only the two chat-history methods ``submit_chat_task`` touches."""

    def __init__(self) -> None:
        self.rows = []

    def append_chat_history(self, row):
        self.rows.append(dict(row))

    def update_chat_history_entry(self, ts, patch):
        self.rows.append({"ts": ts, **dict(patch)})


def test_submit_chat_task_runs_the_quick_lane_on_a_different_thread(monkeypatch):
    """The load-bearing invariant: the lane never runs on the caller's thread.

    ``submit_chat_task`` is the shared submission path for Telegram ``/chat`` and
    the HTTP task API; on the dashboard that caller is the event loop.
    """
    seen = {}
    done = threading.Event()

    def _fake_submit_quick_task(prompt, config, vault, **kwargs):
        seen["thread_ident"] = threading.get_ident()
        seen["thread_name"] = threading.current_thread().name
        done.set()
        return SimpleNamespace(status="success")

    # `_run` re-imports the symbol from the module, so patch the MODULE attr.
    monkeypatch.setattr(quick_task, "submit_quick_task", _fake_submit_quick_task)

    caller_ident = threading.get_ident()
    task_id = direct_task.submit_chat_task(
        "do a thing", lane="quick", config=SimpleNamespace(),
        vault=_RecordingVault(), submitted_via="api")

    assert isinstance(task_id, str) and task_id
    assert done.wait(timeout=10.0), "quick lane never ran"
    assert seen["thread_ident"] != caller_ident
    assert seen["thread_name"].startswith("submit-")


def test_submit_chat_task_returns_before_the_lane_finishes(monkeypatch):
    """Returning immediately is the property that keeps the caller responsive —
    a synchronous version would hold the loop for the whole run, gate poll and
    all. Asserted behaviourally: the lane is still blocked when we return."""
    release = threading.Event()
    entered = threading.Event()

    def _slow_submit_quick_task(prompt, config, vault, **kwargs):
        entered.set()
        release.wait(timeout=10.0)
        return SimpleNamespace(status="success")

    monkeypatch.setattr(quick_task, "submit_quick_task", _slow_submit_quick_task)

    t0 = time.monotonic()
    direct_task.submit_chat_task(
        "do a thing", lane="quick", config=SimpleNamespace(),
        vault=_RecordingVault())
    elapsed = time.monotonic() - t0
    try:
        assert entered.wait(timeout=10.0)
        # the lane is demonstrably still running while we already have control
        assert elapsed < 2.0, elapsed
    finally:
        release.set()


def test_poll_command_choice_is_still_the_blocking_shape_this_note_describes():
    """Guards the NOTE above from going stale.

    Asserted against the SOURCE, not the module attribute: ``tests/conftest.py``
    replaces ``_poll_command_choice`` with a timeout-bounding wrapper of the
    identical signature, so an ``inspect.signature`` check here would pass on the
    wrapper and prove nothing about the real function either way.

    If this ever becomes an ``async def`` / event-driven await, the premise
    correction in this module's docstring no longer holds and must be re-read
    rather than trusted.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(quick_task))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "_poll_command_choice")
    assert isinstance(node, ast.FunctionDef), "became async — re-read the premise"
    sleeps = [
        c for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        and c.func.attr == "sleep"
    ]
    assert sleeps, "no longer a sleep-poll — re-read the premise"


def test_the_gate_poll_fails_closed_on_timeout(monkeypatch):
    """Unchanged behaviour, pinned because it is what makes the block-poll safe
    to leave alone: no operator answer ⇒ None ⇒ the caller denies."""
    import systemu.approval.decision_queue as dq

    class _NeverResolves:
        def __init__(self, vault):
            pass

        def get_resolved_choice(self, dedup_key):
            return None

    monkeypatch.setattr(dq, "OperatorDecisionQueue", _NeverResolves)
    assert quick_task._poll_command_choice(object(), "k", timeout=0.05) is None
