"""Regression tests for v0.8.3 — queue-mode predicate drops the `not isatty()`
co-condition.

The bug being locked down here was:

  In v0.8.0 - v0.8.2, the queue-mode branch in notify_user required BOTH:
    SYSTEMU_DECISION_QUEUE=true  AND  not sys.stdin.isatty()

  But the legacy headless-detection block uses a broader OR-of-signals:
    not isatty()  OR  HEADLESS=1  OR  cp1252 encoding  OR  NON_INTERACTIVE=true

  A background daemon spawned from an interactive PowerShell inherits the
  parent's TTY stdin (so isatty()=True) but its stdout encoding is cp1252.
  Queue-mode SKIPPED (isatty=True), headless TRIGGERED (cp1252=True) →
  every shadow_decision prompt silently auto-selected actions[0] even with
  SYSTEMU_DECISION_QUEUE=true explicitly set by the operator.

  The fix: an explicit operator opt-in (env var) ALWAYS routes through the
  queue, regardless of TTY status.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_queue_mode_fires_when_isatty_true(monkeypatch):
    """v0.8.3 regression: queue-mode must fire even when stdin IS a TTY,
    as long as SYSTEMU_DECISION_QUEUE=true is explicitly set.

    This is the daemon-from-PowerShell scenario: daemon inherits parent's TTY
    stdin so isatty()=True, but operator explicitly opted in via env var.
    Queue-mode must NOT be gated on isatty().
    """
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
    # Force isatty=True (the daemon-from-interactive-shell scenario)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    fake_vault = MagicMock()
    fake_queue = MagicMock()
    fake_queue.get_resolved_choice.return_value = None
    fake_queue.post.return_value = "dec_v083_test"

    from systemu.interface import notifications as N
    from systemu.approval.exceptions import PendingOperatorDecision
    monkeypatch.setattr(N, "_vault", fake_vault)
    monkeypatch.setattr(N, "_get_decision_queue", lambda: fake_queue, raising=False)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    with pytest.raises(PendingOperatorDecision) as exc_info:
        N.notify_user(
            title="New Shadow Recommended",
            message="WeatherDoc",
            actions=["Skip", "Assign to Existing", "Awaken"],
            dedup_key="shadow_decision:test_activity",
        )

    assert exc_info.value.decision_id == "dec_v083_test"
    assert exc_info.value.dedup_key == "shadow_decision:test_activity"
    fake_queue.post.assert_called_once()


def test_queue_mode_still_fires_when_isatty_false(monkeypatch):
    """Continues to work for the original CI / Docker scenario where stdin
    is not a TTY."""
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    fake_queue = MagicMock()
    fake_queue.get_resolved_choice.return_value = None
    fake_queue.post.return_value = "dec_v083_notty"

    from systemu.interface import notifications as N
    from systemu.approval.exceptions import PendingOperatorDecision
    monkeypatch.setattr(N, "_vault", MagicMock())
    monkeypatch.setattr(N, "_get_decision_queue", lambda: fake_queue, raising=False)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    with pytest.raises(PendingOperatorDecision):
        N.notify_user(
            title="x", message="x",
            actions=["Skip", "Forge"],
            dedup_key="tool_forge:test",
        )


def test_queue_mode_skipped_when_env_var_unset(monkeypatch):
    """When SYSTEMU_DECISION_QUEUE is NOT set, queue-mode must not fire.
    Legacy headless path takes over instead (existing behavior — TTY operator
    who hasn't opted in shouldn't be forced into queue mode)."""
    monkeypatch.delenv("SYSTEMU_DECISION_QUEUE", raising=False)
    monkeypatch.setenv("SYSTEMU_HEADLESS", "1")  # force legacy headless path
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    from systemu.interface import notifications as N
    monkeypatch.setattr(N, "_vault", None)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    choice = N.notify_user(
        title="x", message="x",
        actions=["Skip", "Forge"],
    )
    # Legacy headless auto-picks actions[0] = "Skip"
    assert choice == "Skip"


def test_resolved_decision_returned_when_isatty_true(monkeypatch):
    """When a previously-posted decision is already resolved AND isatty=True,
    queue-mode must still return the resolved choice (not fall through to
    legacy headless)."""
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    fake_queue = MagicMock()
    fake_queue.get_resolved_choice.return_value = "Awaken"

    from systemu.interface import notifications as N
    monkeypatch.setattr(N, "_vault", MagicMock())
    monkeypatch.setattr(N, "_get_decision_queue", lambda: fake_queue, raising=False)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    choice = N.notify_user(
        title="x", message="x",
        actions=["Skip", "Assign to Existing", "Awaken"],
        dedup_key="shadow_decision:resolved_activity",
    )

    assert choice == "Awaken"
    fake_queue.get_resolved_choice.assert_called_once_with("shadow_decision:resolved_activity")
