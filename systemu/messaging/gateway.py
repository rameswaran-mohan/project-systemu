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

# ─────────────────────────────────────────────────────────────────────────────
#  Outbound MASK  (R-P1 — no secret ever leaves in a push)
# ─────────────────────────────────────────────────────────────────────────────
#
# ``mask_outbound`` is the single outbound chokepoint: every gateway calls it in
# ``push()`` on the message text AND every button label, so a secret can never
# leave the process in a notification. It is deliberately CONSERVATIVE — it
# redacts header values (Authorization / Cookie) and things that look like a
# credential token (bearer / OpenAI ``sk-…`` / AWS ``AKIA…`` / a JWT / a long
# hex or high-entropy run), and leaves normal prose alone.
#
# The token vocabulary reuses ``runtime.elicitation``'s secret name tokens (the
# same words that force a field URL-mode) so the two secret surfaces stay in sync.

_MASK = "***"

# Token-name words that mark a "<name>: <value>" or "<name>=<value>" pair whose
# VALUE must be redacted. Reused from the elicitation secret detector so the
# outbound mask and the inbound URL-mode split share one vocabulary.
try:  # lazy — keep messaging importable even if the runtime pkg shifts.
    from systemu.runtime.elicitation import _SECRET_NAME_TOKENS as _SECRET_TOKENS
except Exception:  # pragma: no cover - defensive fallback
    _SECRET_TOKENS = (
        "password", "passwd", "secret", "token", "api_key", "apikey",
        "access_key", "private_key", "client_secret", "credential", "auth",
    )

# Header/kv value redaction: "Authorization: …", "Cookie: …", "api_key=…", etc.
# Matches the token word, an optional bearer/basic scheme, then the value run up
# to end-of-line (headers/kv pairs are one value per line).
_HEADER_TOKENS = ("authorization", "cookie", "set-cookie", "proxy-authorization")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(" + "|".join(
        re.escape(t) for t in sorted(set(_HEADER_TOKENS) | set(_SECRET_TOKENS),
                                     key=len, reverse=True)
    ) + r")\b\s*[:=]\s*\S+",
)

# Bearer scheme with a token: "Bearer <token>", "Basic <b64>".
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]{6,}=*")

# Token SHAPES (value-only, no name needed).
_TOKEN_SHAPE_RES = (
    re.compile(r"\bsk-[A-Za-z0-9\-]{8,}"),                 # OpenAI-style sk-…
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),                    # AWS access-key id
    re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"),  # JWT
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),                 # GitHub PAT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),        # Slack token
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),                   # long hex (sha/keys)
)


def mask_outbound(text: str, vault: Any = None) -> str:
    """Redact secret-looking spans in an outbound push string.

    Conservative: redacts header/kv secret values, bearer/basic schemes, and
    token shapes (``sk-…``, ``AKIA…``, JWT, ``ghp_…``, Slack, long hex) to
    ``***``; leaves ordinary prose untouched. Never raises (returns the input
    unchanged on any error).

    ``vault`` (optional) additionally enables the KNOWN-VALUE fence: any whole
    token equal to one of the operator's STORED credential values is redacted,
    whatever it looks like. Every rule above is a SHAPE rule, and no shape rule
    can recognise a shapeless secret — ``hunter2`` and
    ``correcthorsebatterystaple`` pass all of them (measured). Widening the
    shapes was tried and rejected on false-positive grounds; see
    ``runtime.credentials.known_values`` for the numbers. Identity closes what
    resemblance cannot.

    The known-value pass is OPT-IN by argument rather than always-on, because
    it reads the credential store: a caller with no vault handle (and the whole
    installed base of existing call sites) keeps exactly the shipped pure,
    stateless, no-IO behaviour. It fails OPEN — no corpus means shape rules
    only, i.e. the status quo — which is the correct direction HERE only
    because this function's standing contract is that masking must never break
    a push. The promotion fence, whose failure is a persisted secret rather
    than a dropped notification, fails CLOSED instead.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}: {_MASK}", text)
        out = _BEARER_RE.sub(_MASK, out)
        for rx in _TOKEN_SHAPE_RES:
            out = rx.sub(_MASK, out)
        if vault is not None:
            from systemu.runtime.credentials.known_values import redact_known_secrets
            out = redact_known_secrets(out, vault, _MASK)
        return out
    except Exception:  # pragma: no cover - masking must never break a push
        return text


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
