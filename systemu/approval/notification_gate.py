"""NotificationApprovalGate — IApprovalGate adapter around notifications.py.

Zero behaviour change.  Delegates log_event, notify_user, confirm, and
queue_dependency_reminder to the module-level functions in notifications.py so
call sites using IApprovalGate are decoupled from the concrete module.

When Phase 3/4 land (SqliteApprovalGate / RedisApprovalGate), this class is
retired — no call sites change.

Usage:
    import systemu.interface.notifications as _notif
    from systemu.approval.notification_gate import NotificationApprovalGate

    gate: IApprovalGate = NotificationApprovalGate(_notif)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class NotificationApprovalGate:
    """IApprovalGate implementation backed by notifications.py module functions."""

    def __init__(self, notifications_module: Any) -> None:
        """
        Args:
            notifications_module: The systemu.interface.notifications module
                                   (or any object with matching functions).
        """
        self._n = notifications_module

    def log_event(
        self,
        level: str,
        category: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._n.log_event(level, category, message, context)

    def notify_user(
        self,
        title: str,
        message: str,
        actions: List[str],
        *,
        context: Optional[Dict[str, Any]] = None,
        prompt_for_name: bool = False,
    ) -> str:
        return self._n.notify_user(
            title, message, actions,
            context=context, prompt_for_name=prompt_for_name,
        )

    def confirm(self, prompt_text: str, default: bool = True) -> bool:
        return self._n.confirm(prompt_text, default)

    def queue_dependency_reminder(self, tool: Any, vault: Any) -> None:
        self._n.queue_dependency_reminder(tool, vault)
