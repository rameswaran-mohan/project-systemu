"""Notification system — prompts the user via CLI and queues decisions.

For MVP: uses click.confirm() / click.prompt() synchronously in the CLI.
All notifications are also written to vault/notifications/pending.json
so the future NiceGUI dashboard can pick them up asynchronously.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from systemu.core.utils import utcnow

import click
from rich.console import Console
from rich.panel import Panel
from rich import print as rprint

from systemu.core.models import Notification, NotificationStatus

logger = logging.getLogger(__name__)
console = Console()

# Optional vault reference — set by the pipeline caller before notifications fire
_vault: Any = None  # type: ignore[type-arg]
_event_log_path: Any = None  # Path to event_log.jsonl, set when vault is injected

# v0.8.0 Pattern 1: lazy-initialised OperatorDecisionQueue cache.  None until
# the vault is set AND a queue is first requested.  Reset to None by tests via
# monkeypatch so cache invalidation isn't a concern in production.
_decision_queue_instance = None


def _get_decision_queue():
    """Lazy-initialise the OperatorDecisionQueue from the module-level _vault.

    Returns None if the vault isn't set yet (e.g. during very early boot) OR
    if the queue can't be constructed for any reason.  Callers must handle
    None gracefully.
    """
    global _decision_queue_instance
    if _decision_queue_instance is not None:
        return _decision_queue_instance
    if _vault is None:
        return None
    try:
        from systemu.approval.decision_queue import OperatorDecisionQueue
        _decision_queue_instance = OperatorDecisionQueue(_vault)
        return _decision_queue_instance
    except Exception:
        logger.exception("[Notify] could not build decision queue")
        return None


def set_vault(vault: Any, event_log_path: Any = None) -> None:  # noqa: ANN401
    """Inject the vault instance so notifications can be persisted.

    Args:
        vault:          Any IVault-compatible object.
        event_log_path: Optional explicit path for event_log.jsonl.  When
                        omitted, the path is derived from vault.root (file
                        vault) or vault.data_dir (sqlite vault).  Pass an
                        explicit Path to suppress auto-detection entirely.
    """
    global _vault, _event_log_path, _decision_queue_instance
    # v0.8.0 Pattern 1: invalidate the lazily-cached OperatorDecisionQueue so
    # it gets rebuilt against the new vault on next request.
    _decision_queue_instance = None
    _vault = vault

    if event_log_path is not None:
        # Caller supplied the path explicitly — use it as-is
        from pathlib import Path
        _event_log_path = Path(event_log_path)
        _event_log_path.parent.mkdir(parents=True, exist_ok=True)
        return

    try:
        from pathlib import Path
        # File vault exposes .root; SqliteVault exposes .data_dir; fall back to None
        base_dir: Optional[Path] = None
        if hasattr(vault, "root"):
            base_dir = Path(vault.root)
        elif hasattr(vault, "data_dir"):
            base_dir = Path(vault.data_dir)
        elif hasattr(vault, "_memory_dir"):
            # SqliteVault — data_dir is the parent of memory_dir
            base_dir = Path(vault._memory_dir).parent

        if base_dir is not None:
            notif_dir = base_dir / "notifications"
            notif_dir.mkdir(parents=True, exist_ok=True)
            _event_log_path = notif_dir / "event_log.jsonl"
        else:
            _event_log_path = None
    except Exception:
        _event_log_path = None


def log_event(
    level: str,
    category: str,
    message: str,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a structured event to the real-time event log (event_log.jsonl).

    Args:
        level:    "INFO" | "WARNING" | "ERROR" | "SUCCESS"
        category: Short tag, e.g. "scroll" | "shadow" | "tool" | "job" | "system"
        message:  Human-readable description of the event.
        context:  Optional dict with any extra data (IDs, paths, etc.)
    """
    from datetime import datetime
    import json as _json

    entry = {
        "ts": utcnow().isoformat() + "Z",
        "level": level.upper(),
        "category": category,
        "message": message,
        "context": context or {},
    }

    # Write to logger
    log_fn = {
        "INFO": logger.info,
        "SUCCESS": logger.info,
        "WARNING": logger.warning,
        "ERROR": logger.error,
    }.get(level.upper(), logger.info)
    log_fn("[Event:%s] %s — %s", category, level, message)

    # Append to jsonl file
    if _event_log_path is not None:
        try:
            with open(_event_log_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(entry) + "\n")
        except Exception as exc:
            logger.warning("[Notify] Could not write event log: %s", exc)

    # Publish to EventBus for real-time Systemu Chat UI (non-blocking; failures are silenced)
    try:
        from systemu.interface.event_bus import EventBus
        EventBus.get().publish(entry)
    except Exception:
        pass  # EventBus is optional — never break log_event()


def queue_dependency_reminder(tool: Any, vault: Any) -> None:
    """Queue a one-time advisory notification when a tool has declared dependencies.

    Called from any code path that enables a tool — the UI toggle and the
    auto_forge pipeline both route through here.  Never blocks, never installs.
    Deduped per tool_id so repeated calls are safe.

    Args:
        tool:  Tool model instance (must have .id, .name, .dependencies).
        vault: Vault instance (must have list_pending_notifications / queue_notification).
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
                f"Tool \"{tool.name}\" declares the following dependencies:\n"
                f"  {deps_str}\n\n"
                f"Verify they are installed before first use:\n"
                f"  pip install {' '.join(tool.dependencies)}\n\n"
                f"If a package is missing, the tool will fail with a clear install "
                f"instruction in the Event Log."
            ),
            actions=["OK"],
            context={
                "notification_type": "dep_reminder",
                "tool_id":           tool.id,
            },
        )
        vault.queue_notification(notif)
    except Exception:
        pass


def _make_id() -> str:
    from systemu.core.utils import generate_id
    return generate_id("notif")


# ─────────────────────────────────────────────────────────────────────────────

def notify_user(
    title: str,
    message: str,
    actions: List[str],
    *,
    context: Optional[Dict[str, Any]] = None,
    prompt_for_name: bool = False,
    dedup_key: Optional[str] = None,
) -> str:
    """Display a rich CLI notification and wait for the user to choose an action.

    Args:
        title:           Short heading shown in the panel header.
        message:         Body of the notification.
        actions:         List of valid choice strings.  **Ordering contract
                         (v0.6.1-b):** ``actions[0]`` MUST be the safe-by-default
                         choice.  In non-interactive mode (SYSTEMU_NON_INTERACTIVE,
                         no TTY, etc.) the function auto-selects ``actions[0]``
                         without operator input.  Order accordingly:
                           GOOD: ["Skip", "Forge"]      auto-skip is safe
                           GOOD: ["Reject", "Approve"]  auto-reject is safe
                           BAD:  ["Forge", "Skip"]      auto-forge runs LLM code
                           BAD:  ["Approve", "Reject"]  auto-approve = silent yes
        context:         Arbitrary dict persisted with the notification record.
        prompt_for_name: If True, additionally ask for a name string after the choice.
        dedup_key:       Optional v0.8.0+ idempotency key for the OperatorDecisionQueue.
                         When SYSTEMU_DECISION_QUEUE=true is set and stdin is not a TTY,
                         a non-empty dedup_key routes the decision to the dashboard
                         queue and raises PendingOperatorDecision instead of auto-picking
                         actions[0]. Format suggestion: "<caller_id>:<entity_id>" e.g.
                         "tool_forge:tool_abc123".

    Returns:
        The user's chosen action string (always one of `actions`).
    """
    notif = Notification(
        id=_make_id(),
        title=title,
        message=message,
        actions=actions,
        context=context or {},
    )

    # Persist to queue so the web UI can also see it
    if _vault is not None:
        try:
            _vault.queue_notification(notif)
        except Exception as exc:
            logger.warning("Failed to queue notification: %s", exc)

    # ── Auto-accept for confirmation-only notifications ──────────────────────
    # When the caller only offers one action (e.g. ["OK"]), there's no real
    # decision to make — it's a confirmation, not a choice.  Skip the prompt
    # unconditionally so non-interactive pipeline runs don't hang.
    if len(actions) == 1:
        only = actions[0]
        logger.debug("[Notify] Single-option notification — auto-accepting '%s'", only)
        if _vault is not None:
            try:
                _vault.resolve_notification(notif.id, only)
            except Exception:
                pass
        if prompt_for_name:
            return f"{only}:"
        return only

    # ── v0.8.0 Pattern 1: queue-mode branch ──────────────────────────────────
    # When SYSTEMU_DECISION_QUEUE=true AND stdin is not a TTY, route this
    # decision through the OperatorDecisionQueue: persisted in the vault,
    # surfaced on the dashboard /insights page, resolved by an operator
    # click.  The caller is expected to catch PendingOperatorDecision and
    # exit cleanly with a "waiting for operator" message.
    import sys as _sys
    import os as _os
    _decision_queue_enabled = (
        (_os.environ.get("SYSTEMU_DECISION_QUEUE") or "").lower() == "true"
    )
    if _decision_queue_enabled and not _sys.stdin.isatty():
        queue = _get_decision_queue()
        if queue is not None and dedup_key:
            resolved = queue.get_resolved_choice(dedup_key)
            if resolved is not None:
                logger.info(
                    "[Notify] Queue-mode: returning resolved choice %r for %r",
                    resolved, dedup_key,
                )
                if _vault is not None:
                    try:
                        _vault.resolve_notification(notif.id, resolved)
                    except Exception:
                        pass
                return f"{resolved}:" if prompt_for_name else resolved
        elif queue is not None and not dedup_key:
            logger.debug(
                "[Notify] Queue-mode active but no dedup_key passed — "
                "each call will post a new decision. Callers should pass "
                "a stable dedup_key (e.g. 'tool_forge:<tool_id>') to enable "
                "idempotent resolution."
            )
        if queue is not None:
            decision_id = queue.post(
                title=title,
                body=message,
                options=actions,
                context=context or {},
                dedup_key=dedup_key or "",
            )
            from systemu.approval.exceptions import PendingOperatorDecision
            raise PendingOperatorDecision(
                decision_id=decision_id,
                dedup_key=dedup_key or "",
                options=actions,
            )
        # Queue requested but unavailable — fall through to legacy headless
        logger.warning(
            "[Notify] SYSTEMU_DECISION_QUEUE=true but no queue available; "
            "falling back to legacy headless auto-pick."
        )

    # ── Headless detection: if no TTY, auto-select first action ──────────────
    #
    # Defensive checks — `sys.stdin.isatty()` alone is unreliable on Windows
    # in some subprocess contexts (Click runner, Bash-spawned processes
    # inheriting parent terminal handles, etc.) where it returns True even
    # though the process can't actually read user input.  Combine multiple
    # signals plus an explicit env var so operators can force headless mode
    # for CI / automation.
    import sys, os
    _stdout_enc = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
    _console_non_unicode = _stdout_enc.lower().replace("-", "") not in ("utf8", "utf16", "utf32")
    # v0.6.1-b: env renamed from SYSTEMU_AUTO_APPROVE_SCROLLS (which misled
    # operators into thinking it only affected scroll approval).  When set,
    # any multi-action prompt auto-picks actions[0] — callers MUST order
    # actions so the first entry is the SAFE-by-default choice.  See the
    # action-ordering contract on notify_user above.
    non_interactive = (os.environ.get("SYSTEMU_NON_INTERACTIVE") or "").lower() == "true"
    is_headless = (
        not sys.stdin.isatty()
        or os.environ.get("SYSTEMU_HEADLESS") == "1"
        or _console_non_unicode
        or non_interactive
    )
    if is_headless:
        auto_choice = actions[0]
        logger.info(
            "[Notify] Headless mode — auto-selecting '%s' for: %s",
            auto_choice, title,
        )
        # v0.8.0: deprecation warning when SYSTEMU_HEADLESS is set explicitly
        # (does not fire when headless was detected via no-TTY alone).
        if os.environ.get("SYSTEMU_HEADLESS") == "1":
            logger.warning(
                "[Notify] SYSTEMU_HEADLESS=1 is deprecated in v0.8.0 — set "
                "SYSTEMU_DECISION_QUEUE=true to route operator decisions to "
                "the dashboard queue instead of silent auto-pick."
            )
        if _vault is not None:
            try:
                _vault.resolve_notification(notif.id, auto_choice)
            except Exception:
                pass
        if prompt_for_name:
            return f"{auto_choice}:"
        return auto_choice

    # ── Rich display ──────────────────────────────────────────────────────────
    try:
        console.print()
        console.print(Panel(
            f"[bold yellow]{message}[/bold yellow]",
            title=f"[bold cyan]>> Systemu - {title}[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        ))
    except (UnicodeEncodeError, UnicodeDecodeError):
        print(f"\n>> Systemu - {title}\n{message}\n")

    actions_lower = [a.lower() for a in actions]
    actions_display = " / ".join(f"[bold]{a}[/bold]" for a in actions)
    try:
        console.print(f"  Options: {actions_display}")
    except (UnicodeEncodeError, UnicodeDecodeError):
        print(f"  Options: {' / '.join(actions)}")

    choice: str = ""
    while choice not in actions_lower:
        raw = click.prompt(
            f"  Your choice [{'/'.join(actions)}]",
            default=actions[0],
        ).strip().lower()
        choice = raw

        # Allow prefix matching (e.g. "a" for "approve")
        matches = [a for a in actions_lower if a.startswith(choice)]
        if len(matches) == 1:
            choice = matches[0]
        elif choice not in actions_lower:
            console.print(f"  [red]Invalid choice. Please enter one of: {', '.join(actions)}[/red]")
            choice = ""

    resolved = next(a for a in actions if a.lower() == choice)

    # Resolve notification record
    if _vault is not None:
        try:
            _vault.resolve_notification(notif.id, resolved)
        except Exception as exc:
            logger.warning("Failed to resolve notification: %s", exc)

    try:
        console.print(f"  [green]ok {resolved}[/green]\n")
    except (UnicodeEncodeError, UnicodeDecodeError):
        print(f"  ok {resolved}\n")

    if prompt_for_name:
        name = click.prompt("  Enter a name", default="").strip()
        return f"{resolved}:{name}"

    return resolved


def confirm(prompt_text: str, default: bool = True) -> bool:
    """Simple yes/no confirmation wrapper."""
    import sys, os
    if not sys.stdin.isatty() or os.environ.get("SYSTEMU_HEADLESS") == "1":
        return default
    return click.confirm(f"  {prompt_text}", default=default)
