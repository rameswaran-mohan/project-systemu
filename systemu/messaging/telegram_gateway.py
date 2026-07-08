"""Telegram bot gateway — long-poll inbound, push outbound.

Why Telegram first:

* Free bot creation, no OAuth, single bot token.
* Long-polling mode works from behind NAT — perfect for self-hosted
  operators.  No public webhook URL needed.
* Solid Python SDK with file uploads, inline keyboards, message edits.
* Markdown rendering matches the dashboard's chat history format.

The transport uses the official ``python-telegram-bot`` library (v20+),
which is async.  This module exposes a synchronous facade on top so the
existing daemon's threading model doesn't have to change.  The library
is an optional dependency — the gateway gracefully degrades to "not
configured" when it isn't installed.

Security:
    * Strict user-ID allowlist (``SHARING_ON_TELEGRAM_ALLOWED_USER_IDS``).
    * Refusal to start with an empty allowlist.
    * All inbound commands go through the dashboard's existing approval
      gates — chat is a thin operator surface, never a privilege
      escalation.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Set

from .gateway import (
    Gateway,
    InboundCommand,
    InlineButton,
    OutboundMessage,
    allowlist_from_env,
    dispatch,
    mask_outbound,
    parse_command,
)
from .decision_bridge import parse_callback, resolve_from_channel

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  R-P1 inbound: the button-tap → resolver seam
# ─────────────────────────────────────────────────────────────────────────────
#
# ``route_callback`` is the PURE parse+route seam a tapped inline button hits. It
# has NO dependency on a live python-telegram-bot Application, so it is testable
# directly. The PTB ``_handle_callback_query`` adapter (below) is a thin shell
# that extracts the wire fields and calls this. A typed ``/answer`` command
# (``handlers.handle_answer``) feeds the SAME ``resolve_from_channel`` resolver —
# button tap and slash command converge on one server-side gate.

_BAD_BUTTON_REPLY = (
    "Sorry, I couldn't read that button — open the dashboard Inbox."
)


def route_callback(callback_data, sender_id: str) -> tuple[str, str]:
    """Route a Telegram inline-button ``callback_data`` to the resolver.

    ``parse_callback`` first (garbage / unparseable → a friendly no-op that NEVER
    calls the resolver), then ``resolve_from_channel(tag, choice_key,
    sender_id=..., channel="telegram")``. ``sender_id`` is passed THROUGH verbatim
    so the resolver's own allowlist re-check (defense in depth) can fire.

    Returns ``(outcome, reply_text)``. Never raises.
    """
    parsed = parse_callback(callback_data)
    if parsed is None:
        return ("BAD", _BAD_BUTTON_REPLY)
    tag, choice_key = parsed
    return resolve_from_channel(
        tag, choice_key, sender_id=str(sender_id), channel="telegram",
    )


class TelegramGateway:
    """Telegram bot — long-poll inbound, push outbound."""

    platform = "telegram"

    def __init__(
        self,
        *,
        bot_token: str,
        allowlist: Set[str],
        command_handlers: Optional[Dict[str, Callable[[InboundCommand], str]]] = None,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        if not allowlist:
            raise ValueError(
                "Telegram allowlist is empty — refusing to start.  "
                "Set SHARING_ON_TELEGRAM_ALLOWED_USER_IDS to a comma-"
                "separated list of authorised user IDs."
            )
        self.bot_token = bot_token
        self.allowlist = set(allowlist)
        self.command_handlers = command_handlers or {}
        self._app = None             # python-telegram-bot Application, lazily built
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── Public lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        """Spawn a background thread running the bot's poll loop."""
        if self._thread and self._thread.is_alive():
            logger.info("[TelegramGateway] already running — start() is a no-op")
            return

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_poll_loop,
            daemon=True,
            name="telegram-gateway",
        )
        self._thread.start()
        logger.info(
            "[TelegramGateway] started (allowlist=%d user%s)",
            len(self.allowlist), "" if len(self.allowlist) == 1 else "s",
        )

    def stop(self) -> None:
        """Signal the poll loop to exit.  Returns once the thread joined."""
        self._stop.set()
        if self._app is not None:
            try:
                self._app.stop_running()  # python-telegram-bot v20+
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[TelegramGateway] stopped")

    def push(self, message: OutboundMessage) -> None:
        """Send *message* to one or all allowlisted users.

        When ``message.user_id`` is None, broadcasts to every user on
        the allowlist (uses-case: a watchdog alert anyone monitoring
        the bot should see).
        """
        if self._app is None:
            logger.warning(
                "[TelegramGateway] push() called before start() — dropping message",
            )
            return

        # R-P1 MASK chokepoint: redact any secret-looking span before it leaves
        # the process — the message text AND every inline button label. This is
        # the single place EVERY outbound push funnels through, so nothing can
        # bypass the mask.
        message = self._mask_message(message)

        recipients = [message.user_id] if message.user_id else sorted(self.allowlist)
        for user_id in recipients:
            try:
                self._send_to(user_id, message)
            except Exception as exc:
                logger.warning(
                    "[TelegramGateway] failed to push to %s: %s", user_id, exc,
                )

    @staticmethod
    def _mask_message(message: OutboundMessage) -> OutboundMessage:
        """Return a copy of *message* with the text and every inline-button label
        run through :func:`mask_outbound`. Callbacks are opaque tokens (never
        secret-bearing) so they're left intact."""
        masked_buttons = [
            InlineButton(label=mask_outbound(b.label), callback=b.callback)
            for b in (message.inline_buttons or [])
        ]
        return OutboundMessage(
            text=mask_outbound(message.text),
            user_id=message.user_id,
            inline_buttons=masked_buttons,
            category=message.category,
        )

    # ── Internals ──────────────────────────────────────────────────

    def _run_poll_loop(self) -> None:
        """Run the python-telegram-bot Application in this thread."""
        try:
            from telegram.ext import (
                Application,
                CallbackQueryHandler,
                CommandHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            logger.warning(
                "[TelegramGateway] python-telegram-bot not installed — "
                "gateway is dormant.  pip install 'python-telegram-bot>=20'"
            )
            return

        try:
            self._app = Application.builder().token(self.bot_token).build()

            # /command handlers — every command routes through the same
            # parser + dispatch so the same surface works across platforms.
            for cmd in self.command_handlers.keys():
                self._app.add_handler(
                    CommandHandler(cmd, self._wrap_command_handler(cmd))
                )
            # Inline-button taps (parked-decision resolution) → route_callback →
            # the SAME resolve_from_channel a /answer command hits. Registered
            # once, catches every callback query.
            self._app.add_handler(
                CallbackQueryHandler(self._handle_callback_query)
            )
            # Plain-text fallback → /chat
            self._app.add_handler(
                MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_plain_text)
            )

            logger.info("[TelegramGateway] entering poll loop")
            # run_polling is blocking — exits when stop_running() is called.
            self._app.run_polling(stop_signals=None)
        except Exception as exc:
            logger.error("[TelegramGateway] poll loop crashed: %s", exc, exc_info=True)
        finally:
            self._app = None

    def _wrap_command_handler(self, cmd_name: str):
        """Build an async closure that routes through our command dispatch."""
        async def _handler(update, context):  # python-telegram-bot signature
            text = update.message.text or ""
            user_id = str(update.effective_user.id)
            if user_id not in self.allowlist:
                logger.warning(
                    "[TelegramGateway] rejected /%s from non-allowlisted user %s",
                    cmd_name, user_id,
                )
                await update.message.reply_text("Unauthorised.")
                return
            cmd = parse_command(text, user_id=user_id, platform=self.platform)
            if cmd is None:
                return
            reply = dispatch(cmd, self.command_handlers)
            await update.message.reply_text(reply)
        return _handler

    async def _handle_plain_text(self, update, context):
        text = update.message.text or ""
        user_id = str(update.effective_user.id)
        if user_id not in self.allowlist:
            await update.message.reply_text("Unauthorised.")
            return
        cmd = parse_command(text, user_id=user_id, platform=self.platform)
        if cmd is None:
            return
        reply = dispatch(cmd, self.command_handlers)
        await update.message.reply_text(reply)

    async def _handle_callback_query(self, update, context):
        """PTB adapter for an inline-button tap.

        Thin: extract ``callback_query.data`` + ``effective_user.id``, route
        through :func:`route_callback` (which parses + resolves), then ACK the
        tap. Telegram requires every callback query to be answered or the
        client shows a perpetual spinner, so we answer even on the no-op path.
        The reply text is surfaced in the answer toast; on a successful resolve
        we also edit the original message so the operator sees it's done.
        """
        query = update.callback_query
        data = getattr(query, "data", None)
        sender_id = str(update.effective_user.id)
        try:
            outcome, reply = route_callback(data, sender_id)
        except Exception:  # defensive: an adapter must never bubble to PTB
            logger.exception("[TelegramGateway] callback routing crashed")
            outcome, reply = ("BAD", _BAD_BUTTON_REPLY)
        # ACK the tap (dismisses the client spinner); show the reply as a toast.
        try:
            await query.answer(reply)
        except Exception:
            # A too-long toast or a stale query id must not break the flow.
            try:
                await query.answer()
            except Exception:
                logger.debug("[TelegramGateway] callback answer failed",
                             exc_info=True)
        # On a resolved tap, replace the buttons/message so it can't be re-tapped.
        if outcome == "OK":
            try:
                await query.edit_message_text(reply)
            except Exception:
                logger.debug("[TelegramGateway] edit_message_text failed",
                             exc_info=True)

    def _send_to(self, user_id: str, message: OutboundMessage) -> None:
        """Synchronous wrapper around the async send_message call."""
        import asyncio
        if self._app is None:
            return
        coro = self._app.bot.send_message(
            chat_id=int(user_id),
            text=message.text,
            reply_markup=_build_inline_keyboard(message.inline_buttons),
        )
        # Run inside the Application's running loop.
        asyncio.run_coroutine_threadsafe(coro, self._app.bot._loop)


def _build_inline_keyboard(buttons):
    """Convert ``InlineButton`` items into the SDK's keyboard markup."""
    if not buttons:
        return None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return None
    rows = [
        [InlineKeyboardButton(b.label, callback_data=b.callback)]
        for b in buttons
    ]
    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────────
#  Convenience: build the gateway from config + env
# ─────────────────────────────────────────────────────────────────────────────

def build_from_env(
    *,
    command_handlers: Optional[Dict[str, Callable[[InboundCommand], str]]] = None,
) -> Optional["TelegramGateway"]:
    """Inspect env vars and return a configured gateway, or None when off.

    Reads:
        * ``SHARING_ON_TELEGRAM_BOT_TOKEN`` — required to activate
        * ``SHARING_ON_TELEGRAM_ALLOWED_USER_IDS`` — required to start

    Returns ``None`` when the bot token is missing — the daemon treats
    this as "messaging not configured, skip silently".
    """
    import os
    token = os.environ.get("SHARING_ON_TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return None

    allowlist = allowlist_from_env("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS")
    if not allowlist:
        logger.error(
            "[TelegramGateway] bot token present but allowlist empty — "
            "set SHARING_ON_TELEGRAM_ALLOWED_USER_IDS to enable.",
        )
        return None

    return TelegramGateway(
        bot_token=token,
        allowlist=allowlist,
        command_handlers=command_handlers or {},
    )
