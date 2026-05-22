"""Gateway protocol — shared abstraction for chat-platform integrations.

Each concrete gateway (Telegram, Slack, Discord, …) provides:

* A long-running ``start()`` that listens for inbound messages.
* A ``stop()`` for clean shutdown.
* A ``push(message)`` to send operator-facing notifications outwards
  (execution complete, pending approval, watchdog alert).

Inbound messages flow through a single command parser so every
platform has the same surface: ``/chat <prompt>``, ``/status``,
``/approve <scroll_id>``, etc.  This module owns that parser; the
gateways own the transport.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Protocol, Set, runtime_checkable

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Message types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InboundCommand:
    """Normalised inbound message after the command parser ran."""

    user_id: str                 # platform-specific identifier (string for portability)
    command: str                 # canonical command name without the leading "/"
    args: str = ""               # everything after the command word, trimmed
    raw_text: str = ""           # original message text — useful for /chat
    platform: str = "unknown"    # "telegram" | "slack" | "discord" | …


@dataclass
class OutboundMessage:
    """A message the runtime wants to push to an operator."""

    text: str
    user_id: Optional[str] = None     # None = broadcast to every allowlisted user
    inline_buttons: List["InlineButton"] = field(default_factory=list)
    category: str = "info"            # "info" | "approval" | "execution" | "watchdog"


@dataclass
class InlineButton:
    """One button in an inline keyboard.  Platforms render them where they can."""

    label: str
    callback: str                # Opaque token the gateway hands back on click


# ─────────────────────────────────────────────────────────────────────────────
#  Command parser
# ─────────────────────────────────────────────────────────────────────────────

# Canonical command set.  Plain-text messages without a leading "/" are
# treated as `chat <message>` (the most common use case).
_KNOWN_COMMANDS = {
    "chat",
    "status",
    "approve",
    "reject",
    "scrolls",
    "activities",
    "shadows",
    "help",
}


def parse_command(
    text: str,
    *,
    user_id: str,
    platform: str = "unknown",
) -> Optional[InboundCommand]:
    """Turn a raw message string into an :class:`InboundCommand`.

    Returns ``None`` when *text* is empty.  Unknown ``/<word>`` commands
    are still returned — the gateway can decide whether to surface an
    error message or ignore.
    """
    if not text or not text.strip():
        return None

    stripped = text.strip()

    # Plain text without a leading slash → /chat fallback
    if not stripped.startswith("/"):
        return InboundCommand(
            user_id=user_id,
            command="chat",
            args=stripped,
            raw_text=text,
            platform=platform,
        )

    # /command [args...]
    m = re.match(r"^/(\w+)(?:\s+(.*))?$", stripped, flags=re.DOTALL)
    if not m:
        return InboundCommand(
            user_id=user_id, command="help", args="", raw_text=text, platform=platform,
        )

    command = m.group(1).lower()
    args = (m.group(2) or "").strip()
    return InboundCommand(
        user_id=user_id, command=command, args=args, raw_text=text, platform=platform,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Allowlist
# ─────────────────────────────────────────────────────────────────────────────

def allowlist_from_env(env_var: str) -> Set[str]:
    """Parse a comma-separated allowlist env var into a set of user IDs.

    Example::

        SHARING_ON_TELEGRAM_ALLOWED_USER_IDS=12345,67890

    Returns an **empty set** when the env var is unset or empty.  An
    empty allowlist means "reject everyone" — the gateway should refuse
    to start when the allowlist is empty so an unintended public bot is
    impossible.
    """
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        return set()
    return {p.strip() for p in raw.split(",") if p.strip()}


# ─────────────────────────────────────────────────────────────────────────────
#  Gateway protocol
# ─────────────────────────────────────────────────────────────────────────────

@runtime_checkable
class Gateway(Protocol):
    """One way to talk to operators outside the dashboard.

    Concrete implementations live alongside this module
    (``telegram_gateway.py``, etc.).  The protocol is intentionally
    transport-agnostic — concrete classes deal with sockets, polling,
    rate limits, and platform quirks.
    """

    platform: str
    allowlist: Set[str]

    def start(self) -> None:
        """Begin listening.  Blocks (or starts a background thread)."""
        ...

    def stop(self) -> None:
        """Stop listening and release the connection cleanly."""
        ...

    def push(self, message: OutboundMessage) -> None:
        """Send *message* to one or all allowlisted users."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
#  Command dispatch helper
# ─────────────────────────────────────────────────────────────────────────────

CommandHandler = Callable[[InboundCommand], str]


def dispatch(
    cmd: InboundCommand,
    handlers: dict[str, CommandHandler],
    *,
    fallback: Optional[CommandHandler] = None,
) -> str:
    """Route an inbound command to the matching handler.

    Args:
        cmd:       The parsed inbound command.
        handlers:  Mapping ``{command_name: handler}``.  Handlers receive
                   the :class:`InboundCommand` and return a string reply.
        fallback:  Called when no handler matches.  Defaults to a
                   "command not recognised" message.

    Returns the reply text to send back through the gateway.
    """
    handler = handlers.get(cmd.command)
    if handler is None:
        if fallback is not None:
            return fallback(cmd)
        known = ", ".join(sorted(_KNOWN_COMMANDS))
        return (
            f"Unknown command /{cmd.command}.  Try /help.  "
            f"Known: {known}."
        )
    try:
        return handler(cmd)
    except Exception as exc:
        logger.exception("[Gateway] handler for /%s raised", cmd.command)
        return f"Sorry — /{cmd.command} failed: {exc}"
