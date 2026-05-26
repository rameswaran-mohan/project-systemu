"""Tests for the v0.8.0 notify_user queue-mode branch (Pattern 1)."""
from unittest.mock import MagicMock, patch
import pytest


def test_queue_mode_returns_resolved_choice_when_present(monkeypatch):
    """When SYSTEMU_DECISION_QUEUE=true and a resolved decision exists for
    the dedup_key, notify_user returns the resolved choice transparently."""
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    fake_vault = MagicMock()
    fake_queue = MagicMock()
    fake_queue.get_resolved_choice.return_value = "Forge"

    from systemu.interface import notifications as N
    monkeypatch.setattr(N, "_vault", fake_vault)
    monkeypatch.setattr(N, "_get_decision_queue", lambda: fake_queue, raising=False)
    # Reset the lazy cache so the monkeypatched helper is hit
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    choice = N.notify_user(
        title="Forge?",
        message="Want to forge tool_x?",
        actions=["Skip", "Forge"],
        dedup_key="tool_forge:tool_x",
    )
    assert choice == "Forge"
    fake_queue.get_resolved_choice.assert_called_once_with("tool_forge:tool_x")


def test_queue_mode_raises_pending_when_no_resolution(monkeypatch):
    """When SYSTEMU_DECISION_QUEUE=true and the decision is fresh / pending,
    notify_user raises PendingOperatorDecision and persists the decision."""
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    fake_vault = MagicMock()
    fake_queue = MagicMock()
    fake_queue.get_resolved_choice.return_value = None
    fake_queue.post.return_value = "dec_new"

    from systemu.interface import notifications as N
    from systemu.approval.exceptions import PendingOperatorDecision
    monkeypatch.setattr(N, "_vault", fake_vault)
    monkeypatch.setattr(N, "_get_decision_queue", lambda: fake_queue, raising=False)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    with pytest.raises(PendingOperatorDecision) as exc_info:
        N.notify_user(
            title="x", message="x",
            actions=["Skip", "Forge"],
            dedup_key="tool_forge:tool_x",
        )
    assert exc_info.value.decision_id == "dec_new"
    assert exc_info.value.dedup_key == "tool_forge:tool_x"
    fake_queue.post.assert_called_once()


def test_legacy_headless_still_works_when_decision_queue_disabled(monkeypatch):
    """When SYSTEMU_DECISION_QUEUE is NOT set but SYSTEMU_HEADLESS=1,
    notify_user falls back to the legacy auto-pick (returns actions[0])."""
    monkeypatch.delenv("SYSTEMU_DECISION_QUEUE", raising=False)
    monkeypatch.setenv("SYSTEMU_HEADLESS", "1")
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    from systemu.interface import notifications as N
    monkeypatch.setattr(N, "_vault", None)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)

    choice = N.notify_user(
        title="x", message="x",
        actions=["Skip", "Forge"],
    )
    assert choice == "Skip"  # actions[0]


def test_tty_path_still_uses_click_prompt(monkeypatch):
    """When stdin IS a TTY, notify_user still uses click.prompt regardless
    of SYSTEMU_DECISION_QUEUE."""
    monkeypatch.setenv("SYSTEMU_DECISION_QUEUE", "true")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # Make the stdout-encoding heuristic not flip to headless
    import sys
    if not getattr(sys.stdout, "encoding", None):
        monkeypatch.setattr(sys.stdout, "encoding", "utf-8", raising=False)
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)
    monkeypatch.delenv("SYSTEMU_NON_INTERACTIVE", raising=False)

    from systemu.interface import notifications as N
    monkeypatch.setattr(N, "_vault", None)
    monkeypatch.setattr(N, "_decision_queue_instance", None, raising=False)
    with patch("click.prompt", return_value="Forge") as mock_prompt:
        choice = N.notify_user(
            title="x", message="x",
            actions=["Skip", "Forge"],
        )
    assert choice == "Forge"
    mock_prompt.assert_called_once()
