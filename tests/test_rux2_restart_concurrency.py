"""R-UX2 — the "Restart Workers" click, and the concurrency the fix introduced.

Getting the 500ms settle off the event loop meant making the click handler
``async``. That is the right fix for UX-9(a) — a sync handler froze the whole
dashboard for half a second per click — but it is not free, and the first cut
did not pay for it:

``nicegui/events.py`` runs a **sync** handler inline (``result = handler()``,
so the loop itself serialises clicks) and hands an **async** one to
``background_tasks.create_or_defer``. The moment the handler became a coroutine,
two quick clicks could overlap. ``Supervisor.start()`` was NOT idempotent — it
constructed and started two fresh threads unconditionally — so the overlap left
a DUPLICATE dispatcher and heartbeat running for the life of the process. That
is the FIX-3/FIX-4 double-cleanup class, reached from a button.

Both halves are pinned here, against a REAL ``Supervisor`` (the concrete type
production passes) rather than a stand-in: idempotency at the object, and
serialisation at the handler.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
import types
from unittest import mock

import pytest

from systemu.runtime.supervisor import Supervisor


@pytest.fixture
def supervisor(tmp_path):
    """A real Supervisor on a temp vault, NOT started, with the process-wide
    singleton restored afterwards (``__init__`` claims it unconditionally).

    Use this only for the cold-start cases. Anything that exercises the RESTART
    button must use ``running_supervisor`` — see its docstring.
    """
    cfg = types.SimpleNamespace(vault_dir=tmp_path)
    vault = types.SimpleNamespace(root=str(tmp_path))
    previous = Supervisor._instance
    sup = Supervisor(cfg, vault)
    # Threads already alive before this test — see `_extra_supervisor_threads`.
    sup._test_thread_baseline = frozenset(_supervisor_threads())
    try:
        yield sup
    finally:
        sup._shutdown_event.set()
        # Bounded by the SAME worst case production uses, not by a smaller
        # round number. This teardown used to join for 3.0s, which is the very
        # mistake the code under test is about: the dispatcher blocks in
        # `queue.get(timeout=2.0)` and again in `semaphore.acquire(timeout=2.0)`,
        # so 3.0s is not long enough to be a stop. Under full-suite load it
        # routinely timed out and LEAKED a live dispatcher+heartbeat pair into
        # every later test in this file — which is how three of them failed in
        # the suite while passing in isolation.
        survivors = []
        deadline = time.monotonic() + Supervisor.RESTART_JOIN_TIMEOUT_S
        for t in (sup._dispatcher_thread, sup._heartbeat_thread):
            if t is not None:
                t.join(timeout=max(0.0, deadline - time.monotonic()))
                if t.is_alive():
                    survivors.append(t.name)
        Supervisor._instance = previous
        # Fail LOUD. A fixture that leaks a worker silently corrupts every test
        # after it, and the corruption surfaces far from its cause.
        assert not survivors, (
            f"fixture leaked live supervisor threads: {survivors} — later tests "
            f"in this file will see them in the process-wide census")


@pytest.fixture
def running_supervisor(supervisor):
    """A supervisor whose workers are ALREADY RUNNING — the state the Restart
    Workers button is actually pressed in.

    **This is the coverage gap that let the restart regression ship.** The tests
    that drove the click handler used the un-started fixture, so they entered
    with ``_dispatcher_thread is None`` and took the COLD path: ``start()``
    created a thread because there was none, and "a thread exists afterwards"
    was satisfied by one that had never been restarted. The defect —
    ``start()``'s ``is_alive()`` idempotency guard PRESERVING a running
    dispatcher instead of replacing it — was structurally unobservable, because
    the fixture pre-supplied exactly the state (no thread) that hides it.

    It also registers itself as the singleton, because ``_force_restart_workers``
    reaches production's ``Supervisor.get()``.
    """
    supervisor.start()
    assert supervisor._dispatcher_thread.is_alive()
    assert supervisor._heartbeat_thread.is_alive()
    Supervisor._instance = supervisor
    return supervisor


def _supervisor_threads() -> list:
    return [t for t in threading.enumerate()
            if t.name in ("supervisor-dispatcher", "supervisor-heartbeat")
            and t.is_alive()]


def _extra_supervisor_threads(sup) -> list:
    """Supervisor threads beyond the ones already alive when ``sup`` was built.

    The census is process-wide and name-based, because the duplicate-drain bug
    this file exists to catch produces a thread that NOTHING still references —
    an instance-attribute check cannot see it. But a process-wide count is not a
    fact this test owns: any other supervisor alive in the interpreter is in it.
    So the assertions are on the DELTA, which is the number this test actually
    created, and they keep their power to catch a duplicate.

    Matched by thread IDENTITY, not by position: ``threading.enumerate()`` gives
    no ordering guarantee, so slicing off a baseline COUNT would silently
    compare the wrong threads.
    """
    baseline = getattr(sup, "_test_thread_baseline", frozenset())
    return [t for t in _supervisor_threads() if t not in baseline]


# ── idempotency at the object ────────────────────────────────────────────────

def test_repeated_start_does_not_stack_up_duplicate_threads(supervisor):
    """MUTATION TARGET. Before the fix this produced six live threads for three
    calls — two dispatchers draining one queue, two heartbeats writing one set
    of stamps, and no way to stop the extras."""
    baseline = len(_supervisor_threads())
    supervisor.start()
    first = (supervisor._dispatcher_thread, supervisor._heartbeat_thread)
    assert all(t.is_alive() for t in first)

    supervisor.start()
    supervisor.start()

    assert supervisor._dispatcher_thread is first[0]
    assert supervisor._heartbeat_thread is first[1]
    assert len(_supervisor_threads()) - baseline == 2, [
        t.name for t in _supervisor_threads()]


def test_concurrent_starts_from_many_threads_still_yield_one_pair(supervisor):
    """The click path is genuinely concurrent now, so the guard has to be a
    LOCK and not merely an ``if`` — eight racing callers must not each see
    "no thread yet" and start their own."""
    baseline = len(_supervisor_threads())
    barrier = threading.Barrier(8)
    errors = []

    def _racer():
        try:
            barrier.wait(timeout=10.0)
            supervisor.start()
        except Exception as exc:            # pragma: no cover - diagnostic
            errors.append(exc)

    racers = [threading.Thread(target=_racer) for _ in range(8)]
    for t in racers:
        t.start()
    for t in racers:
        t.join(timeout=10.0)

    assert not errors, errors
    assert len(_supervisor_threads()) - baseline == 2, [
        t.name for t in _supervisor_threads()]


def test_a_half_dead_pair_is_repaired_not_left_alone(supervisor):
    """The guard is per-thread, not "did I ever start?".

    A blanket ``if self._running: return`` would make the heartbeat
    unrecoverable after it died — which is exactly the failure the Restart
    Workers button exists to clear.
    """
    supervisor.start()
    live_dispatcher = supervisor._dispatcher_thread
    assert live_dispatcher.is_alive()

    dead = threading.Thread(target=lambda: None, name="already-finished")
    dead.start()
    dead.join(timeout=5.0)
    assert not dead.is_alive()
    supervisor._heartbeat_thread = dead

    supervisor.start()

    assert supervisor._dispatcher_thread is live_dispatcher, "restarted a live thread"
    assert supervisor._heartbeat_thread is not dead, "left the dead thread in place"
    assert supervisor._heartbeat_thread.is_alive()


def test_start_still_starts_from_cold(supervisor):
    """The negative half: an idempotency guard that never starts anything would
    pass every test above."""
    assert supervisor._dispatcher_thread is None
    supervisor.start()
    assert supervisor._dispatcher_thread is not None
    assert supervisor._dispatcher_thread.is_alive()
    assert supervisor._heartbeat_thread.is_alive()


# ── serialisation at the handler ─────────────────────────────────────────────

class _RecordingUI:
    def __init__(self):
        self.messages = []

    def notify(self, message, **kw):
        self.messages.append((str(message), kw.get("type")))


def _fake_nicegui():
    ui = _RecordingUI()

    async def _io_bound(fn, *a, **k):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*a, **k))

    module = types.ModuleType("nicegui")
    module.ui = ui
    module.run = types.SimpleNamespace(io_bound=_io_bound)
    return module, ui


def test_the_restart_handler_is_async_so_the_click_yields_the_loop():
    """The one REAL loop blocker this release found: ``_force_restart_workers``
    is wired as ``on_click`` on the Console page and contains a 500ms settle. As
    a sync handler that ran on the asyncio event loop — a half-second dashboard
    freeze per click, ten times the UX-9(a) budget."""
    from systemu.interface.pages import console
    assert inspect.iscoroutinefunction(console._force_restart_workers)


def test_the_restart_button_is_actually_wired_to_that_handler():
    """Otherwise the handler could be correct and unreferenced — the dropped
    call-site class. Asserted on the AST: a substring search would also match
    the name in a docstring or comment."""
    import ast

    from systemu.interface.pages import console
    tree = ast.parse(inspect.getsource(console))
    wired = {
        kw.value.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "on_click" and isinstance(kw.value, ast.Name)
    }
    assert "_force_restart_workers" in wired, sorted(wired)


def _click(sup):
    """Drive the REAL click handler once and return the recorded notifications."""
    from systemu.interface.pages import console

    Supervisor._instance = sup
    module, ui = _fake_nicegui()
    real = sys.modules.get("nicegui")
    sys.modules["nicegui"] = module
    try:
        asyncio.run(console._force_restart_workers())
    finally:
        if real is not None:
            sys.modules["nicegui"] = real
        else:
            sys.modules.pop("nicegui", None)
    return ui.messages


# ── THE REGRESSION: a restart must actually replace the workers ──────────────

def test_the_restart_button_REPLACES_both_running_threads(running_supervisor):
    """MUTATION TARGET. Reproduced on this box before the fix, 7 runs in 8:

        DISPATCHER RESTARTED? False
        HEARTBEAT RESTARTED?  True
        UI said: [('Workers restarted successfully.', 'positive')]

    ``_restart()`` did ``set()`` -> ``sleep(0.5)`` -> ``clear()`` -> ``start()``,
    and ``start()`` skips any thread that ``is_alive()``. But ``Event.set()``
    does not interrupt ``self._queue.get(timeout=2.0)``, so the dispatcher
    routinely outlived the 0.5s settle and was therefore SKIPPED — while the
    heartbeat, which waits ON the event, died promptly and was replaced. The one
    worker the operator pressed the button to clear was the one preserved.

    Asserted on thread OBJECT IDENTITY, not on ``is_alive()``: a thread that was
    never touched is alive too, which is exactly how the regression passed.
    """
    before_d = running_supervisor._dispatcher_thread
    before_h = running_supervisor._heartbeat_thread

    messages = _click(running_supervisor)

    assert running_supervisor._dispatcher_thread is not before_d, (
        "the DISPATCHER was not replaced — this is the regression")
    assert running_supervisor._heartbeat_thread is not before_h, (
        "the HEARTBEAT was not replaced")
    assert running_supervisor._dispatcher_thread.is_alive()
    assert running_supervisor._heartbeat_thread.is_alive()
    # the ones it replaced are really gone, not leaked alongside the new pair
    assert not before_d.is_alive()
    assert not before_h.is_alive()
    extra = _extra_supervisor_threads(running_supervisor)
    assert len(extra) == 2, [t.name for t in extra]
    assert [t for _m, t in messages] == ["positive"], messages


def test_the_restart_predicate_is_not_the_idempotency_guard(running_supervisor):
    """``start()`` must NOT be what the restart path calls.

    The distinction is the whole bug: ``start()``'s per-thread ``is_alive()``
    check is an IDEMPOTENCY guard (don't stack duplicates) and is correct as
    such — but "is it alive?" is not "should it be replaced?", because a wedged
    thread is alive. Pinned behaviourally: calling ``start()`` on a running
    supervisor must change nothing, while ``restart_workers()`` must replace
    both.
    """
    before = (running_supervisor._dispatcher_thread,
              running_supervisor._heartbeat_thread)

    running_supervisor.start()
    assert running_supervisor._dispatcher_thread is before[0], "start() restarted"
    assert running_supervisor._heartbeat_thread is before[1], "start() restarted"

    replaced = running_supervisor.restart_workers()

    assert replaced == {"supervisor-dispatcher": True, "supervisor-heartbeat": True}
    assert running_supervisor._dispatcher_thread is not before[0]
    assert running_supervisor._heartbeat_thread is not before[1]


def test_a_dispatcher_that_will_not_stop_is_neither_reused_nor_duplicated(
        running_supervisor):
    """MUTATION TARGET. A GENUINELY wedged dispatcher — one that never looks at
    ``_shutdown_event`` — is the case the button exists for and the case the
    regression handled worst.

    Two things must hold, and they pull in opposite directions:
      * it must NOT be silently kept and reported as restarted (the regression); and
      * a replacement must NOT be started on top of it either, because two
        threads draining one queue is the FIX-3/FIX-4 duplicate-drain bug that
        base ``start()`` produced unconditionally.

    So it is left in place and the operator is TOLD. The healthy notify type is
    unreachable on this path.
    """
    # stop the real dispatcher, then install one that ignores shutdown entirely
    release = threading.Event()
    running_supervisor._shutdown_event.set()
    running_supervisor._dispatcher_thread.join(timeout=10.0)
    running_supervisor._shutdown_event.clear()

    wedged = threading.Thread(target=lambda: release.wait(120), daemon=True,
                              name="supervisor-dispatcher")
    wedged.start()
    running_supervisor._dispatcher_thread = wedged
    before_h = running_supervisor._heartbeat_thread

    try:
        messages = _click(running_supervisor)

        assert running_supervisor._dispatcher_thread is wedged, (
            "a second dispatcher was started over a live one")
        assert len([t for t in _extra_supervisor_threads(running_supervisor)
                    if t.name == "supervisor-dispatcher"]) == 1
        # the half that CAN be repaired still is
        assert running_supervisor._heartbeat_thread is not before_h
        assert running_supervisor._heartbeat_thread.is_alive()

        kinds = [t for _m, t in messages]
        assert "positive" not in kinds, (
            "reported success while a worker was left wedged")
        assert kinds == ["negative"], messages
        assert "supervisor-dispatcher" in messages[0][0], messages
    finally:
        release.set()
        wedged.join(timeout=10.0)


def test_restart_workers_joins_rather_than_sleeping_a_fixed_settle(
        running_supervisor):
    """The mechanism, not just the outcome.

    A fixed sleep cannot be a stop: it either guesses too short (the regression
    — 0.5s against a 2.0s queue timeout) or wastes the operator's time. The
    dispatcher is only replaced here because it was really JOINED, so the old
    object must be dead the instant ``restart_workers`` returns — no grace
    period, no "it will exit shortly".
    """
    before_d = running_supervisor._dispatcher_thread
    running_supervisor.restart_workers()
    assert not before_d.is_alive(), (
        "returned while the old dispatcher was still running — not a join")


def test_two_overlapping_clicks_do_not_both_tear_the_workers_down(
        running_supervisor):
    """The regression the async conversion introduced, driven end to end.

    Both coroutines are launched together exactly as ``create_or_defer`` would.
    One performs the restart; the other must be TOLD it was ignored rather than
    silently queueing a second teardown behind the first — a second
    ``_shutdown_event.set()`` landing inside the first's clear/start window
    kills the workers it has just restarted.

    Driven from a RUNNING supervisor: on the cold fixture this reduced to two
    calls that each had no threads to tear down, so the teardown collision it
    names could not occur.
    """
    from systemu.interface.pages import console

    module, ui = _fake_nicegui()

    async def _both():
        await asyncio.gather(console._force_restart_workers(),
                             console._force_restart_workers())

    real = sys.modules.get("nicegui")
    sys.modules["nicegui"] = module
    try:
        asyncio.run(_both())
    finally:
        if real is not None:
            sys.modules["nicegui"] = real
        else:
            sys.modules.pop("nicegui", None)

    kinds = [t for _m, t in ui.messages]
    assert kinds.count("positive") == 1, ui.messages
    assert kinds.count("warning") == 1, ui.messages
    # and the workers are up afterwards, not left shut down
    assert running_supervisor._dispatcher_thread.is_alive()
    assert running_supervisor._heartbeat_thread.is_alive()
    assert not running_supervisor._shutdown_event.is_set()
    extra = _extra_supervisor_threads(running_supervisor)
    assert len(extra) == 2, [t.name for t in extra]


def test_a_result_naming_no_workers_does_not_render_as_success(running_supervisor):
    """``all({}.values())`` is vacuously True.

    A restart that replaced NOTHING must not be able to emit the healthy notify
    just because there is nothing in the result to contradict it — the failure
    path may not reach the healthy value by default.
    """
    from systemu.interface.pages import console

    with mock.patch.object(Supervisor, "restart_workers", return_value={}):
        messages = _click(running_supervisor)

    kinds = [t for _m, t in messages]
    assert "positive" not in kinds, messages
    assert kinds == ["negative"], messages


def test_a_single_click_still_reports_success(running_supervisor):
    """The negative half — a handler that always reported "already in progress",
    or always "incomplete", would pass the tests above."""
    messages = _click(running_supervisor)
    assert [t for _m, t in messages] == ["positive"], messages
    assert running_supervisor._dispatcher_thread.is_alive()
    assert running_supervisor._heartbeat_thread.is_alive()
