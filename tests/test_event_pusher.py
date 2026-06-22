"""Unit tests for EventPusher (Item 11) — EventBus → messaging push.

Pure-Python coverage of translation + rate limiting.  Gateway is mocked
so tests don't depend on the Telegram SDK.
"""

from __future__ import annotations

import time
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from systemu.messaging.event_pusher import EventPusher, translate_event
from systemu.messaging.gateway import OutboundMessage


# ── translate_event — pure function ─────────────────────────────────────

def test_translate_approval_event_always_pushes():
    msg = translate_event({
        "category": "approval",
        "message": "Approve this scroll?",
        "context": {
            "approval_message": "scroll_abc was refined.",
            "options": ["Approve", "Reject"],
        },
    })
    assert msg is not None
    assert msg.category == "approval"
    assert "Approval needed" in msg.text
    assert "/approve" in msg.text


def test_translate_shadow_completion_pushes():
    msg = translate_event({
        "category": "shadow",
        "level": "INFO",
        "message": "MarkdownManager finished",
        "context": {"status": "completed", "shadow_id": "sh-1"},
    })
    assert msg is not None
    assert msg.category == "execution"
    assert "finished" in msg.text


def test_translate_shadow_failure_pushes():
    msg = translate_event({
        "category": "shadow",
        "level": "INFO",
        "message": "TrialScreener failed",
        "context": {"status": "failed", "shadow_id": "sh-1"},
    })
    assert msg is not None
    assert "failed" in msg.text.lower()


def test_translate_shadow_error_level_pushes():
    msg = translate_event({
        "category": "shadow",
        "level": "ERROR",
        "message": "Tool call returned an unrecoverable error",
        "context": {"shadow_id": "sh-1"},
    })
    assert msg is not None
    assert "error" in msg.text.lower()


def test_translate_shadow_iteration_is_dropped():
    """Per-iteration shadow events should NOT push (would spam)."""
    msg = translate_event({
        "category": "shadow",
        "level": "INFO",
        "message": "iteration 3: TOOL_CALL file_write",
        "context": {"shadow_id": "sh-1"},
    })
    assert msg is None


def test_translate_supervisor_watchdog_pushes():
    msg = translate_event({
        "category": "supervisor",
        "level": "WARNING",
        "message": "Shadow stuck — re-queued after 5min heartbeat timeout",
        "context": {},
    })
    assert msg is not None
    assert msg.category == "watchdog"


def test_translate_supervisor_heartbeat_is_dropped():
    msg = translate_event({
        "category": "supervisor",
        "level": "INFO",
        "message": "heartbeat tick",
        "context": {},
    })
    assert msg is None


def test_translate_tool_forge_proposal_pushes():
    msg = translate_event({
        "category": "tool_forge",
        "level": "INFO",
        "message": "New tool proposed: weather_fetch",
        "context": {},
    })
    assert msg is not None
    assert "proposed" in msg.text.lower()


def test_translate_unknown_category_drops():
    msg = translate_event({
        "category": "totally_unknown",
        "message": "...",
        "context": {},
    })
    assert msg is None


def test_translate_empty_event_drops():
    assert translate_event({}) is None


# ── EventPusher.subscribe + rate limiting ──────────────────────────────

class _FakeBus:
    """Minimal EventBus stand-in — replays a list of events to a callback."""

    def __init__(self) -> None:
        self.callbacks: List = []

    def subscribe(self, cb, *, replay=False):
        self.callbacks.append(cb)
        def _unsub():
            try:
                self.callbacks.remove(cb)
            except ValueError:
                pass
        return _unsub

    def emit(self, event):
        for cb in list(self.callbacks):
            cb(event)


@pytest.fixture
def pusher_setup():
    """Wire a pusher to a fake bus + mock gateway."""
    bus = _FakeBus()
    gateway = MagicMock()
    pusher = EventPusher(gateway)
    pusher.subscribe(bus)
    return pusher, bus, gateway


def test_pusher_routes_event_to_gateway(pusher_setup):
    pusher, bus, gateway = pusher_setup
    bus.emit({
        "category": "shadow",
        "message": "done",
        "context": {"status": "completed"},
    })
    assert gateway.push.call_count == 1
    pushed = gateway.push.call_args[0][0]
    assert isinstance(pushed, OutboundMessage)
    assert "finished" in pushed.text


def test_pusher_skips_irrelevant_events(pusher_setup):
    pusher, bus, gateway = pusher_setup
    bus.emit({"category": "shadow", "message": "iter 1",
              "context": {"shadow_id": "sh-1"}})
    bus.emit({"category": "supervisor", "message": "heartbeat", "context": {}})
    assert gateway.push.call_count == 0


def test_pusher_rate_limits_per_category(pusher_setup):
    pusher, bus, gateway = pusher_setup
    # Use a tight limit to exercise the window: 3 per 60s.
    pusher.rate_limits = {"shadow": (3, 60), "supervisor": (0, 0), "approval": (0, 0)}
    for _ in range(5):
        bus.emit({
            "category": "shadow",
            "message": "done",
            "context": {"status": "completed"},
        })
    assert gateway.push.call_count == 3   # 4th + 5th dropped


def test_pusher_rate_limit_window_recovers():
    """After the window elapses the next event passes through again."""
    bus = _FakeBus()
    gateway = MagicMock()
    pusher = EventPusher(
        gateway,
        rate_limits={"shadow": (1, 1)},   # 1 push per 1s
    )
    pusher.subscribe(bus)

    bus.emit({"category": "shadow", "message": "1",
              "context": {"status": "completed"}})
    bus.emit({"category": "shadow", "message": "2",
              "context": {"status": "completed"}})
    assert gateway.push.call_count == 1

    time.sleep(1.05)

    bus.emit({"category": "shadow", "message": "3",
              "context": {"status": "completed"}})
    assert gateway.push.call_count == 2


def test_pusher_approval_is_unlimited(pusher_setup):
    """Approvals are operator-driven and must always fire."""
    pusher, bus, gateway = pusher_setup
    for _ in range(20):
        bus.emit({
            "category": "approval",
            "message": "Approve?",
            "context": {"approval_message": "scroll_x"},
        })
    assert gateway.push.call_count == 20


def test_pusher_translator_exception_doesnt_break_bus(pusher_setup):
    """A buggy translator must not crash the event bus."""
    pusher, bus, gateway = pusher_setup
    pusher.translator = lambda e: (_ for _ in ()).throw(RuntimeError("boom"))
    # Should not raise.
    bus.emit({"category": "shadow", "message": "x", "context": {}})
    assert gateway.push.call_count == 0


def test_pusher_gateway_failure_is_caught(pusher_setup):
    """If the gateway.push() raises, the bus keeps running."""
    pusher, bus, gateway = pusher_setup
    gateway.push.side_effect = RuntimeError("network down")
    # Should not raise.
    bus.emit({"category": "shadow", "message": "done",
              "context": {"status": "completed"}})
    assert gateway.push.call_count == 1


def test_pusher_shutdown_unsubscribes(pusher_setup):
    pusher, bus, gateway = pusher_setup
    assert len(bus.callbacks) == 1
    pusher.shutdown()
    assert len(bus.callbacks) == 0


def test_pusher_subscribe_is_idempotent(pusher_setup):
    pusher, bus, gateway = pusher_setup
    pusher.subscribe(bus)   # second subscribe is a no-op
    bus.emit({"category": "shadow", "message": "done",
              "context": {"status": "completed"}})
    # Still one push, not two.
    assert gateway.push.call_count == 1
