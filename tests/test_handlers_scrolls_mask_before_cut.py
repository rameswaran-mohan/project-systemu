"""DEC-31 on the phone's /scrolls reply: redact BEFORE the 60-char cut.

``handle_scrolls`` shows each scroll name cut to 60 characters. The push masks the
whole reply, but only after the cut: a secret straddling the cap is split into a
prefix no shape rule recognises (a 40+ char hex run cut to 14), and the piece
goes out in clear. The secret here is LONGER than the cap (70 chars), placed so the
cut lands inside it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from systemu.messaging import handlers
from systemu.messaging.gateway import InboundCommand, mask_outbound

_CAP = 60
# 70 hex chars: longer than the cap, and the long-hex shape rule (40+) only matches
# it whole, so a cut that leaves fewer than 40 of them is invisible to the mask.
_SECRET = "a3c9e1f07b52d84c6e0a9f13b7d25c48e1a06f9d3b84c27e5a10d96f3c8b24e71a05d8"
assert len(_SECRET) == 70


def _cmd():
    return InboundCommand(user_id="1", command="scrolls")


def _reply(monkeypatch, name):
    vault = SimpleNamespace(load_index=lambda kind: [
        {"id": "s1", "status": "approved", "name": name}])
    monkeypatch.setattr(handlers, "_state", lambda: SimpleNamespace(vault=vault))
    return handlers.handle_scrolls(_cmd())


def _no_piece_survives(secret, text, width=10):
    return not any(secret[i:i + width] in text
                   for i in range(len(secret) - width + 1))


def test_the_fixture_straddles_the_cap_and_defeats_a_cut_then_mask():
    name = "x" * 45 + " " + _SECRET
    assert len(_SECRET) > _CAP and 45 < _CAP < 45 + 1 + len(_SECRET)
    # The pre-fix order, on purpose: cut, then mask. The cut line is held in a
    # variable (the DEC-31 lint's escape hatch is pinned to the one DEC-31
    # characterisation file, so this demonstration does not use it) -- the
    # 14-char prefix escapes every shape rule, which is the hazard the fix is
    # measured against.
    cut = name[:_CAP]
    assert not _no_piece_survives(_SECRET, mask_outbound(cut))
    # the fixed order: mask, then cut -> nothing of it survives
    assert _no_piece_survives(_SECRET, mask_outbound(name)[:_CAP])


@pytest.mark.parametrize("lead", [0, 20, 45, 55])
def test_scrolls_reply_leaks_no_piece_of_a_secret_that_straddles_the_cut(monkeypatch, lead):
    name = "x" * lead + (" " if lead else "") + _SECRET
    reply = _reply(monkeypatch, name)
    assert "Recent scrolls:" in reply and "s1" in reply
    assert _no_piece_survives(_SECRET, reply)
    # and the line is still capped: the name field is never longer than the cap
    line = [ln for ln in reply.splitlines() if "s1" in ln][0]
    assert len(line.split(" — ", 2)[2]) <= _CAP


def test_an_ordinary_name_is_still_cut_to_the_cap_and_otherwise_unchanged(monkeypatch):
    name = "Weekly report for the finance team " * 4
    reply = _reply(monkeypatch, name)
    assert name[:_CAP] in reply and name[:_CAP + 1] not in reply


def test_a_non_string_name_does_not_break_the_listing(monkeypatch):
    reply = _reply(monkeypatch, 12345)
    assert "12345" in reply and "Could not load scrolls" not in reply
