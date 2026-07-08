"""R-P1 Telegram INBOUND — the two operator entry points that both feed the SAME
server-side resolver (``decision_bridge.resolve_from_channel``):

  1. TAPPING an inline button  → PTB ``CallbackQueryHandler`` → ``route_callback``.
  2. Typing ``/answer <tag> <a1..a4>`` → ``handlers.handle_answer``.

The pure parse+route logic lives in seams that need NO live python-telegram-bot
Application:

  * ``telegram_gateway.route_callback(callback_data, sender_id) -> (outcome, reply)``
    — ``parse_callback`` (garbage → friendly no-op, never calls resolve) then
    ``resolve_from_channel(tag, key, sender_id=..., channel="telegram")``.
  * ``handlers.handle_answer(cmd) -> reply`` — split ``cmd.args`` into
    ``<tag> <a1..a4>``, call ``resolve_from_channel``.

Both tests monkeypatch ``resolve_from_channel`` (imported into each module's
namespace) with a spy so we assert routing WITHOUT a vault/queue. The PTB
``_handle_callback_query`` is a thin adapter tested with a fake update object.
"""
from __future__ import annotations

import asyncio

import pytest

from systemu.messaging import telegram_gateway as tg
from systemu.messaging import handlers as h
from systemu.messaging.gateway import InboundCommand


# ── shared spy ────────────────────────────────────────────────────────────────

class _ResolveSpy:
    """Records the calls to resolve_from_channel and returns a canned outcome."""
    def __init__(self, outcome="OK", msg="Done — resolved."):
        self.calls = []
        self._outcome = outcome
        self._msg = msg

    def __call__(self, tag, choice, *, sender_id, channel, **kw):
        self.calls.append({"tag": tag, "choice": choice,
                           "sender_id": sender_id, "channel": channel})
        return (self._outcome, self._msg)


# ── route_callback (button-tap seam) ──────────────────────────────────────────

def test_route_callback_resolves(monkeypatch):
    # A valid d|tag|a1 from an allowlisted sender → calls
    # resolve_from_channel(tag, "a1", sender_id=..., channel="telegram") and
    # returns its outcome + message unchanged.
    spy = _ResolveSpy(outcome="OK", msg="Done — resolved as “Approve”.")
    monkeypatch.setattr(tg, "resolve_from_channel", spy)

    outcome, reply = tg.route_callback("d|k3f7qa|a1", "111")

    assert outcome == "OK"
    assert reply == "Done — resolved as “Approve”."
    assert spy.calls == [{"tag": "k3f7qa", "choice": "a1",
                          "sender_id": "111", "channel": "telegram"}]


def test_route_callback_garbage_no_crash(monkeypatch):
    # "not-a-token" → parse_callback returns None → route returns a no-op friendly
    # message, never raises, and NEVER calls resolve_from_channel.
    spy = _ResolveSpy()
    monkeypatch.setattr(tg, "resolve_from_channel", spy)

    outcome, reply = tg.route_callback("not-a-token", "111")

    assert outcome == "BAD"
    assert "couldn't read that button" in reply
    assert spy.calls == []   # resolve was never reached


def test_route_callback_none_data_no_crash(monkeypatch):
    # A None callback_data (defensive) must also no-op, not raise.
    spy = _ResolveSpy()
    monkeypatch.setattr(tg, "resolve_from_channel", spy)
    outcome, reply = tg.route_callback(None, "111")
    assert outcome == "BAD"
    assert spy.calls == []


def test_callback_from_non_allowlisted_ignored(monkeypatch):
    # The gateway gates too, but the adapter must pass sender_id THROUGH so
    # resolve_from_channel's own allowlist re-check can fire. We assert the
    # sender_id reaches resolve unchanged (resolve itself returns the refusal).
    spy = _ResolveSpy(outcome="UNKNOWN_TAG",
                      msg="I don't have anything for you to resolve.")
    monkeypatch.setattr(tg, "resolve_from_channel", spy)

    outcome, reply = tg.route_callback("d|k3f7qa|a2", "999")

    assert outcome == "UNKNOWN_TAG"
    assert spy.calls[0]["sender_id"] == "999"   # passed through verbatim


# ── _handle_callback_query (thin PTB adapter) ─────────────────────────────────

class _FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.answered_with = "__unset__"
        self.edited_to = None

    async def answer(self, text=None, **kw):
        self.answered_with = text

    async def edit_message_text(self, text, **kw):
        self.edited_to = text


class _FakeUser:
    def __init__(self, uid):
        self.id = uid


class _FakeUpdate:
    def __init__(self, data, uid):
        self.callback_query = _FakeCallbackQuery(data)
        self.effective_user = _FakeUser(uid)


def test_handle_callback_query_adapter(monkeypatch):
    # The async adapter extracts update.callback_query.data +
    # update.effective_user.id, routes through route_callback, and acknowledges
    # the tap via query.answer(). It must not raise.
    spy = _ResolveSpy(outcome="OK", msg="Done — resolved.")
    monkeypatch.setattr(tg, "resolve_from_channel", spy)

    gw = tg.TelegramGateway(bot_token="x", allowlist={"111"})
    update = _FakeUpdate("d|k3f7qa|a1", 111)

    asyncio.run(gw._handle_callback_query(update, context=None))

    # sender_id was stringified from the int user id.
    assert spy.calls == [{"tag": "k3f7qa", "choice": "a1",
                          "sender_id": "111", "channel": "telegram"}]
    # The tap was acknowledged (PTB requires answering a callback query).
    assert update.callback_query.answered_with is not None


def test_handle_callback_query_garbage_still_answers(monkeypatch):
    # Garbage data must not crash the adapter and must still ack the tap.
    spy = _ResolveSpy()
    monkeypatch.setattr(tg, "resolve_from_channel", spy)
    gw = tg.TelegramGateway(bot_token="x", allowlist={"111"})
    update = _FakeUpdate("garbage", 111)
    asyncio.run(gw._handle_callback_query(update, context=None))
    assert spy.calls == []
    assert update.callback_query.answered_with is not None


# ── handle_answer (/answer <tag> <choice> seam) ───────────────────────────────

def test_answer_command_routes_same_path(monkeypatch):
    # /answer k3f7qa a1 → resolve_from_channel(tag, "a1", sender_id="123",
    # channel="telegram"), returning its human message.
    spy = _ResolveSpy(outcome="OK", msg="Done — resolved as “Approve”.")
    monkeypatch.setattr(h, "resolve_from_channel", spy)

    reply = h.handle_answer(InboundCommand(
        user_id="123", command="answer", args="k3f7qa a1"))

    assert reply == "Done — resolved as “Approve”."
    assert spy.calls == [{"tag": "k3f7qa", "choice": "a1",
                          "sender_id": "123", "channel": "telegram"}]


def test_answer_bad_args(monkeypatch):
    # /answer with a missing choice → a helpful usage string, and resolve is
    # NEVER called.
    spy = _ResolveSpy()
    monkeypatch.setattr(h, "resolve_from_channel", spy)

    reply = h.handle_answer(InboundCommand(
        user_id="123", command="answer", args="k3f7qa"))

    assert "Usage" in reply or "usage" in reply
    assert "answer" in reply.lower()
    assert spy.calls == []


def test_answer_empty_args(monkeypatch):
    spy = _ResolveSpy()
    monkeypatch.setattr(h, "resolve_from_channel", spy)
    reply = h.handle_answer(InboundCommand(user_id="123", command="answer", args=""))
    assert "Usage" in reply or "usage" in reply
    assert spy.calls == []


def test_answer_numeric_choice_maps_to_axn(monkeypatch):
    # Friendlier: a bare numeric choice (1..4) maps to a{n}. "/answer tag 2" -> a2.
    spy = _ResolveSpy(outcome="OK", msg="Done.")
    monkeypatch.setattr(h, "resolve_from_channel", spy)
    reply = h.handle_answer(InboundCommand(
        user_id="123", command="answer", args="k3f7qa 2"))
    assert reply == "Done."
    assert spy.calls[0]["choice"] == "a2"


def test_answer_out_of_range_numeric_rejected(monkeypatch):
    # A numeric choice outside 1..4 is not a valid key → usage hint, no resolve.
    spy = _ResolveSpy()
    monkeypatch.setattr(h, "resolve_from_channel", spy)
    reply = h.handle_answer(InboundCommand(
        user_id="123", command="answer", args="k3f7qa 9"))
    assert "Usage" in reply or "usage" in reply
    assert spy.calls == []


def test_answer_missing_user_id_passes_empty(monkeypatch):
    # sender_id must always be a string for resolve's allowlist check; a falsy
    # user_id becomes "" (which will fail the allowlist, correctly).
    spy = _ResolveSpy(outcome="UNKNOWN_TAG", msg="nope")
    monkeypatch.setattr(h, "resolve_from_channel", spy)
    reply = h.handle_answer(InboundCommand(
        user_id="", command="answer", args="k3f7qa a1"))
    assert spy.calls[0]["sender_id"] == ""


# ── default_handlers wiring ───────────────────────────────────────────────────

def test_answer_in_default_handlers():
    handlers = h.default_handlers()
    assert "answer" in handlers
    assert handlers["answer"] is h.handle_answer


def test_help_mentions_answer():
    reply = h.handle_help(InboundCommand(user_id="1", command="help"))
    assert "/answer" in reply
