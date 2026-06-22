"""Phase 5 Slice 1, amendment A1 — the header "Needs you (N)" badge model.

The Phase-4 right rail hides below 1100px and the sidebar collapses at 768px,
so the header badge is the ONLY narrow-viewport path to parked harness gates.
``needs_you_badge_model`` is the pure model behind it: count from
``InboxQueue(vault).list_descriptors()``, hidden at 0, always targeting
``/inbox``, and defensive — ANY failure must yield 0/hidden, never an
exception (the badge can never break the page shell).
"""
from __future__ import annotations

import systemu.interface.command.inbox as inbox_mod
from systemu.interface.dashboard import needs_you_badge_model


class _FakeQueue:
    """InboxQueue stand-in returning a fixed descriptor list."""

    descriptors: list = []

    def __init__(self, vault):
        self._vault = vault

    def list_descriptors(self):
        return list(type(self).descriptors)


class _ExplodingInit:
    def __init__(self, vault):
        raise RuntimeError("vault unreachable")


class _ExplodingList:
    def __init__(self, vault):
        pass

    def list_descriptors(self):
        raise OSError("decision store corrupt")


def test_counts_pending_gates_and_is_visible(monkeypatch):
    _FakeQueue.descriptors = [("d1", object()), ("d2", object()), ("d3", object())]
    monkeypatch.setattr(inbox_mod, "InboxQueue", _FakeQueue)
    model = needs_you_badge_model(vault=object())
    assert model == {"count": 3, "visible": True, "target": "/inbox"}


def test_zero_pending_is_hidden(monkeypatch):
    _FakeQueue.descriptors = []
    monkeypatch.setattr(inbox_mod, "InboxQueue", _FakeQueue)
    model = needs_you_badge_model(vault=object())
    assert model == {"count": 0, "visible": False, "target": "/inbox"}


def test_queue_constructor_failure_is_hidden_not_raised(monkeypatch):
    monkeypatch.setattr(inbox_mod, "InboxQueue", _ExplodingInit)
    model = needs_you_badge_model(vault=object())
    assert model == {"count": 0, "visible": False, "target": "/inbox"}


def test_list_descriptors_failure_is_hidden_not_raised(monkeypatch):
    monkeypatch.setattr(inbox_mod, "InboxQueue", _ExplodingList)
    model = needs_you_badge_model(vault=object())
    assert model == {"count": 0, "visible": False, "target": "/inbox"}


def test_none_vault_is_hidden_not_raised(monkeypatch):
    # No fake: the real InboxQueue(None) path must also degrade to 0/hidden.
    model = needs_you_badge_model(vault=None)
    assert model["count"] == 0
    assert model["visible"] is False
    assert model["target"] == "/inbox"
