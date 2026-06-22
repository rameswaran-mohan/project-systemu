"""IApprovalGate — backend-agnostic interface for user-facing notifications.

Implementations:
  NotificationApprovalGate — wraps notifications.py (CLI prompts + vault queue)
  SqliteApprovalGate        — approvals table + polling (Phase 3, cross-process)
  RedisApprovalGate         — Redis BLPOP (Phase 4, cross-machine)

Covers the three user-facing notification functions that pipeline stages,
scheduler jobs, and shadow_runtime call:  log_event, notify_user, confirm.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class IApprovalGate(Protocol):
    """User notification and approval interface."""

    def log_event(
        self,
        level: str,
        category: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a structured event to the event log and publish to EventBroker.

        level:    "INFO" | "WARNING" | "ERROR" | "SUCCESS"
        category: e.g. "scroll" | "shadow" | "tool" | "job" | "system"
        """
        ...

    def notify_user(
        self,
        title: str,
        message: str,
        actions: List[str],
        *,
        context: Optional[Dict[str, Any]] = None,
        prompt_for_name: bool = False,
    ) -> str:
        """Display a notification and wait for the user to choose an action.

        In CLI mode: rich panel + click.prompt.
        In headless mode: auto-selects actions[0].
        In dashboard mode (future): publishes approval event, blocks on response.

        Returns the chosen action string.
        """
        ...

    def confirm(self, prompt_text: str, default: bool = True) -> bool:
        """Simple yes/no confirmation.  Auto-approves in headless mode."""
        ...

    def queue_dependency_reminder(self, tool: Any, vault: Any) -> None:
        """Queue a one-time advisory notification for a tool's declared dependencies.

        Deduped per tool_id — safe to call multiple times.
        """
        ...
