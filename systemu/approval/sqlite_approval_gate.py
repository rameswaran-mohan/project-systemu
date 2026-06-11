"""SqliteApprovalGate — IApprovalGate backed by SqliteEventBroker.

Bridges the approval/notification interface (IApprovalGate) to the
cross-process approval mechanism already baked into SqliteEventBroker.

Architecture
------------
  Worker process calls gate.notify_user(...)
    → SqliteEventBroker.request_approval(...)   [non-blocking INSERT]
    → blocks-polls ApprovalRow until status="resolved" or timeout
    → returns chosen action string

  Dashboard process shows pending approvals (read ApprovalRow via list_pending_approvals)
    → user clicks "Approve" / "Reject"
    → AppState.events.resolve_approval(request_id, choice)
    → worker unblocks, gets choice

  log_event() → SqliteEventBroker.publish() to events table + local bus
  confirm()   → headless auto-default (worker has no TTY)
  queue_dependency_reminder() → vault.queue_notification() (non-blocking advisory)

Usage
-----
    gate = SqliteApprovalGate(broker=app_state.events, vault=app_state.vault)
    # Satisfies IApprovalGate — pass to any pipeline stage or shadow runtime.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from systemu.core.utils import utcnow

logger = logging.getLogger(__name__)


class SqliteApprovalGate:
    """IApprovalGate implementation backed by SqliteEventBroker.

    Args:
        broker: A SqliteEventBroker instance (or any IEventBroker that also
                exposes request_approval / resolve_approval).  All approval
                requests are written to the shared DB via this broker so the
                dashboard process can resolve them.
        vault:  Any IVault-compatible object.  Used only by
                queue_dependency_reminder() to persist advisory notifications.
    """

    def __init__(self, broker: Any, vault: Any) -> None:
        self._broker = broker
        self._vault  = vault

    # ── IApprovalGate: log_event ──────────────────────────────────────────────

    def log_event(
        self,
        level: str,
        category: str,
        message: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish a structured event to the broker (DB + local bus).

        Satisfies the "write to event_log" contract via cross-process delivery.
        Also logs to the standard Python logger at the matching level.
        """
        entry = {
            "ts":       utcnow().isoformat() + "Z",
            "level":    level.upper(),
            "category": category,
            "message":  message,
            "context":  context or {},
        }

        # Python logger
        log_fn = {
            "INFO":    logger.info,
            "SUCCESS": logger.info,
            "WARNING": logger.warning,
            "ERROR":   logger.error,
        }.get(level.upper(), logger.info)
        log_fn("[Event:%s] %s — %s", category, level, message)

        # Publish to broker → persisted to events table + bridged to dashboard
        try:
            self._broker.publish(entry)
        except Exception as exc:
            logger.warning("[SqliteApprovalGate] log_event publish failed: %s", exc)

    # ── IApprovalGate: notify_user ────────────────────────────────────────────

    def notify_user(
        self,
        title: str,
        message: str,
        actions: List[str],
        *,
        context: Optional[Dict[str, Any]] = None,
        prompt_for_name: bool = False,
    ) -> str:
        """Route an approval request through the cross-process approval gate.

        Worker process behaviour
        ------------------------
        - If a TTY is attached *and* SYSTEMU_HEADLESS is not set, falls back to
          CLI prompts (same UX as NotificationApprovalGate).  This lets the
          gate work correctly during development when the worker and dashboard
          run in the same process / same terminal.
        - In headless mode (no TTY or SYSTEMU_HEADLESS=1), writes an ApprovalRow
          to the shared DB and blocks-polls until the dashboard user resolves it
          or the timeout expires, then returns the chosen action string.

        Returns the chosen action string (always one of `actions`).
        """
        default_choice = actions[0] if actions else ""

        # ── Headless / cross-process mode ─────────────────────────────────────
        # Wave 1.1: one shared detector (also honours SYSTEMU_NON_INTERACTIVE,
        # which this gate previously ignored).
        from systemu.interface.notifications import is_headless as _is_headless

        if not _is_headless():
            # Interactive fallback — useful when running worker + dashboard in
            # the same process during development / local testing.
            return self._cli_prompt(title, message, actions,
                                    context=context,
                                    prompt_for_name=prompt_for_name)

        # ── Cross-process path ────────────────────────────────────────────────
        # Generate a stable, unique request ID that survives process restarts.
        from systemu.core.utils import generate_id
        request_id = generate_id("approval")

        logger.info(
            "[SqliteApprovalGate] Requesting approval — id=%s title=%r",
            request_id, title,
        )

        choice = self._broker.request_approval(
            request_id=request_id,
            title=title,
            message=message,
            options=actions,
            context=context or {},
            timeout_s=float(os.environ.get("SYSTEMU_APPROVAL_TIMEOUT", "120")),
            default=default_choice,
        )

        if prompt_for_name:
            # Cross-process prompt-for-name is not supported; return bare choice.
            return f"{choice}:"

        return choice

    # ── IApprovalGate: confirm ────────────────────────────────────────────────

    def confirm(self, prompt_text: str, default: bool = True) -> bool:
        """Yes/no confirmation.

        Headless (worker daemon): auto-returns `default`.
        Interactive (dev): falls back to click.confirm.
        """
        from systemu.interface.notifications import is_headless as _is_headless
        if _is_headless():
            logger.info(
                "[SqliteApprovalGate] Headless confirm — auto-%s: %s",
                "yes" if default else "no", prompt_text,
            )
            return default

        try:
            import click
            return click.confirm(f"  {prompt_text}", default=default)
        except Exception:
            return default

    # ── IApprovalGate: queue_dependency_reminder ──────────────────────────────

    def queue_dependency_reminder(self, tool: Any, vault: Any) -> None:
        """Queue a one-time advisory notification for a tool's declared dependencies.

        Deduped per tool_id — safe to call multiple times.  Uses the vault
        passed here (not the one in __init__) to match existing call-site
        conventions in NotificationApprovalGate.
        """
        if not getattr(tool, "dependencies", None):
            return
        try:
            existing = any(
                n.get("context", {}).get("notification_type") == "dep_reminder"
                and n.get("context", {}).get("tool_id") == tool.id
                for n in vault.list_pending_notifications()
            )
            if existing:
                return
            from systemu.core.models import Notification
            from systemu.core.utils import generate_id
            deps_str = ", ".join(tool.dependencies)
            notif = Notification(
                id=generate_id("notif"),
                title=f"Dependency reminder: {tool.name}",
                message=(
                    f'Tool "{tool.name}" declares the following dependencies:\n'
                    f"  {deps_str}\n\n"
                    f"Verify they are installed before first use:\n"
                    f"  pip install {' '.join(tool.dependencies)}\n\n"
                    f"If a package is missing, the tool will fail with a clear "
                    f"install instruction in the Event Log."
                ),
                actions=["OK"],
                context={
                    "notification_type": "dep_reminder",
                    "tool_id":           tool.id,
                },
            )
            vault.queue_notification(notif)
        except Exception:
            pass  # Advisory — never raise

    # ── Internal: CLI fallback ────────────────────────────────────────────────

    def _cli_prompt(
        self,
        title: str,
        message: str,
        actions: List[str],
        *,
        context: Optional[Dict[str, Any]] = None,
        prompt_for_name: bool = False,
    ) -> str:
        """Interactive rich panel + click.prompt (dev / single-process mode)."""
        try:
            import click
            from rich.console import Console
            from rich.panel import Panel
            _console = Console()
        except ImportError:
            # Fallback if rich/click not available
            print(f"\n[{title}] {message}")
            return actions[0] if actions else ""

        _console.print()
        _console.print(Panel(
            f"[bold yellow]{message}[/bold yellow]",
            title=f"[bold cyan]Systemu — {title}[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        ))

        actions_lower = [a.lower() for a in actions]
        actions_display = " / ".join(f"[bold]{a}[/bold]" for a in actions)
        _console.print(f"  Options: {actions_display}")

        choice: str = ""
        while choice not in actions_lower:
            raw = click.prompt(
                f"  Your choice [{'/'.join(actions)}]",
                default=actions[0],
            ).strip().lower()
            choice = raw
            matches = [a for a in actions_lower if a.startswith(choice)]
            if len(matches) == 1:
                choice = matches[0]
            elif choice not in actions_lower:
                _console.print(
                    f"  [red]Invalid. Enter one of: {', '.join(actions)}[/red]"
                )
                choice = ""

        resolved = next(a for a in actions if a.lower() == choice)
        _console.print(f"  [green]Selected: {resolved}[/green]\n")

        if prompt_for_name:
            name = click.prompt("  Enter a name", default="").strip()
            return f"{resolved}:{name}"

        return resolved
