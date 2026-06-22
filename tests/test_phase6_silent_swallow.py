"""Phase 6c — silent-swallowed state mutations now log (representative path).

``queue_dependency_reminder`` defensively wraps its whole body in
``try/except`` so enabling a tool never breaks on a notification-queue
hiccup.  Previously the except did a bare ``pass``, so a real failure to
persist the dep-reminder notification vanished without a trace.  The fix
keeps the graceful guard (no exception propagates) but emits a warning so
the swallowed failure is observable.
"""
import logging

from systemu.interface.notifications import queue_dependency_reminder


class _FakeTool:
    id = "tool_phase6"
    name = "Phase6 Tool"
    dependencies = ["requests"]


class _RaisingVault:
    def list_pending_notifications(self):
        return []

    def queue_notification(self, notif):
        raise RuntimeError("queue is down")


def test_queue_dependency_reminder_logs_swallowed_failure(caplog):
    tool = _FakeTool()
    vault = _RaisingVault()

    with caplog.at_level(logging.WARNING, logger="systemu.interface.notifications"):
        # Must NOT raise — the defensive guard stays.
        queue_dependency_reminder(tool, vault)

    # ...but the swallowed failure must now be observable in the log.
    assert any(
        rec.levelno >= logging.WARNING for rec in caplog.records
    ), "expected a WARNING (or higher) log for the swallowed queue_notification failure"
