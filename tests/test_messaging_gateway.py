"""Unit tests for the messaging gateway (Phase 3).

Pure-Python coverage of the platform-agnostic surface — command
parsing, allowlist resolution, dispatch.  Tests for the Telegram
transport itself are deferred (need a live bot or a heavyweight
mock of the python-telegram-bot Application).
"""

from __future__ import annotations

import pytest

from systemu.messaging.gateway import (
    InboundCommand,
    OutboundMessage,
    allowlist_from_env,
    dispatch,
    parse_command,
)


# ── parse_command ──────────────────────────────────────────────────────

def test_parse_empty_returns_none():
    assert parse_command("", user_id="u1") is None
    assert parse_command("   \n  ", user_id="u1") is None


def test_parse_slash_command():
    cmd = parse_command("/status", user_id="u1", platform="telegram")
    assert cmd is not None
    assert cmd.command == "status"
    assert cmd.args == ""
    assert cmd.user_id == "u1"
    assert cmd.platform == "telegram"


def test_parse_slash_command_with_args():
    cmd = parse_command("/approve scroll_abc123", user_id="u1")
    assert cmd is not None
    assert cmd.command == "approve"
    assert cmd.args == "scroll_abc123"


def test_parse_chat_command():
    cmd = parse_command("/chat tell me about the moon", user_id="u1")
    assert cmd is not None
    assert cmd.command == "chat"
    assert cmd.args == "tell me about the moon"


def test_parse_plain_text_becomes_chat():
    """Messages without a leading / fall through to /chat."""
    cmd = parse_command("hello there", user_id="u1")
    assert cmd is not None
    assert cmd.command == "chat"
    assert cmd.args == "hello there"


def test_parse_multiline_text():
    text = "line one\nline two"
    cmd = parse_command(text, user_id="u1")
    assert cmd is not None
    assert cmd.command == "chat"
    assert "line one" in cmd.args
    assert "line two" in cmd.args


def test_parse_command_lowercases():
    cmd = parse_command("/STATUS", user_id="u1")
    assert cmd is not None
    assert cmd.command == "status"


def test_parse_unknown_command_passes_through():
    """Unknown /<word> commands return as-is for the dispatcher to handle."""
    cmd = parse_command("/wat", user_id="u1")
    assert cmd is not None
    assert cmd.command == "wat"


# ── allowlist_from_env ──────────────────────────────────────────────────

def test_allowlist_empty_when_env_unset(monkeypatch):
    monkeypatch.delenv("MY_ALLOWLIST", raising=False)
    assert allowlist_from_env("MY_ALLOWLIST") == set()


def test_allowlist_parses_comma_separated(monkeypatch):
    monkeypatch.setenv("MY_ALLOWLIST", "123,456,789")
    assert allowlist_from_env("MY_ALLOWLIST") == {"123", "456", "789"}


def test_allowlist_trims_whitespace(monkeypatch):
    monkeypatch.setenv("MY_ALLOWLIST", " 123 , 456 ,789 ")
    assert allowlist_from_env("MY_ALLOWLIST") == {"123", "456", "789"}


def test_allowlist_skips_empty_entries(monkeypatch):
    monkeypatch.setenv("MY_ALLOWLIST", "123,,456,")
    assert allowlist_from_env("MY_ALLOWLIST") == {"123", "456"}


# ── dispatch ───────────────────────────────────────────────────────────

def test_dispatch_routes_to_matching_handler():
    handlers = {
        "ping": lambda c: f"pong: {c.args}",
        "echo": lambda c: c.args,
    }
    cmd = InboundCommand(user_id="u1", command="ping", args="hi")
    assert dispatch(cmd, handlers) == "pong: hi"


def test_dispatch_unknown_returns_help_message():
    handlers = {"ping": lambda c: "pong"}
    cmd = InboundCommand(user_id="u1", command="bogus")
    reply = dispatch(cmd, handlers)
    assert "Unknown command" in reply
    assert "/bogus" in reply


def test_dispatch_calls_fallback_when_set():
    handlers = {"ping": lambda c: "pong"}
    fallback = lambda c: f"FALLBACK: /{c.command}"
    cmd = InboundCommand(user_id="u1", command="bogus")
    assert dispatch(cmd, handlers, fallback=fallback) == "FALLBACK: /bogus"


def test_dispatch_catches_handler_errors():
    """A handler that raises shouldn't crash the gateway."""
    def explode(_cmd):
        raise RuntimeError("kaboom")
    handlers = {"explode": explode}
    cmd = InboundCommand(user_id="u1", command="explode")
    reply = dispatch(cmd, handlers)
    assert "failed" in reply.lower()
    assert "kaboom" in reply


# ── OutboundMessage / TelegramGateway build_from_env ────────────────────

def test_outbound_message_defaults():
    msg = OutboundMessage(text="hello")
    assert msg.text == "hello"
    assert msg.user_id is None    # broadcast
    assert msg.inline_buttons == []
    assert msg.category == "info"


def test_telegram_build_from_env_disabled_when_no_token(monkeypatch):
    monkeypatch.delenv("SHARING_ON_TELEGRAM_BOT_TOKEN", raising=False)
    from systemu.messaging.telegram_gateway import build_from_env
    assert build_from_env() is None


def test_telegram_build_from_env_disabled_when_no_allowlist(monkeypatch, caplog):
    monkeypatch.setenv("SHARING_ON_TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.delenv("SHARING_ON_TELEGRAM_ALLOWED_USER_IDS", raising=False)
    from systemu.messaging.telegram_gateway import build_from_env
    with caplog.at_level("ERROR"):
        assert build_from_env() is None
    assert any("allowlist empty" in r.message for r in caplog.records)


def test_telegram_gateway_requires_token():
    from systemu.messaging.telegram_gateway import TelegramGateway
    with pytest.raises(ValueError, match="bot_token is required"):
        TelegramGateway(bot_token="", allowlist={"123"})


def test_telegram_gateway_requires_allowlist():
    from systemu.messaging.telegram_gateway import TelegramGateway
    with pytest.raises(ValueError, match="allowlist is empty"):
        TelegramGateway(bot_token="fake", allowlist=set())


def test_telegram_gateway_constructs_with_valid_inputs():
    from systemu.messaging.telegram_gateway import TelegramGateway
    gw = TelegramGateway(
        bot_token="fake-token",
        allowlist={"123", "456"},
        command_handlers={"ping": lambda c: "pong"},
    )
    assert gw.platform == "telegram"
    assert gw.allowlist == {"123", "456"}
    assert "ping" in gw.command_handlers
