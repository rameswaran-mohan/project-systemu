"""Messaging gateways — operate Systemu from chat platforms.

Today: Telegram (long-poll, no public webhook needed).  Slack and
Discord are planned as straightforward follow-ups — they share the
same Gateway abstraction defined here so adding a platform is a single
new module under this package.

Activation:
    The gateway is opt-in.  Setting ``SHARING_ON_TELEGRAM_BOT_TOKEN``
    in ``.env`` boots the Telegram gateway from the daemon; without
    the token the gateway is dormant and zero behaviour changes for
    existing users.

See ``docs/messaging.md`` for the operator-facing setup walkthrough.
"""

from __future__ import annotations

from .gateway import Gateway, OutboundMessage, InboundCommand, allowlist_from_env
from .event_pusher import EventPusher, translate_event


__all__ = [
    "Gateway",
    "OutboundMessage",
    "InboundCommand",
    "allowlist_from_env",
    "EventPusher",
    "translate_event",
]
