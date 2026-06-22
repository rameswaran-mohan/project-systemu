"""v0.9.7 Phase 4.3b — headless destructive-confirm must not crash on EOF.

A daemon can inherit a *dead* controlling terminal so ``sys.stdin.isatty()``
returns True yet ``click.confirm`` immediately hits EOF. ``confirm()`` must
fall back to ``default`` (destructive gates pass ``default=False`` ⇒ deny)
instead of letting EOFError crash the run."""
import sys

import click
import pytest

from systemu.interface import notifications


class _FakeTTYStdin:
    """Pretends to be an interactive terminal so confirm() reaches click."""
    def isatty(self):
        return True


def test_confirm_eof_returns_default(monkeypatch):
    # Look like a real TTY and NOT explicit-headless, so the function reaches
    # the click.confirm() call …
    monkeypatch.setattr(sys, "stdin", _FakeTTYStdin())
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)

    # … but the prompt cannot actually be read.
    def _raise_eof(*a, **k):
        raise EOFError("no stdin")
    monkeypatch.setattr(notifications.click, "confirm", _raise_eof)

    # destructive gate semantics: default=False → EOF means deny
    assert notifications.confirm("Delete everything?", default=False) is False
    # and a benign default propagates too
    assert notifications.confirm("Proceed?", default=True) is True


def test_confirm_click_abort_returns_default(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeTTYStdin())
    monkeypatch.delenv("SYSTEMU_HEADLESS", raising=False)

    def _abort(*a, **k):
        raise click.Abort()
    monkeypatch.setattr(notifications.click, "confirm", _abort)

    assert notifications.confirm("risky?", default=False) is False


def test_confirm_headless_env_short_circuits(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeTTYStdin())  # even with a "TTY" …
    monkeypatch.setenv("SYSTEMU_HEADLESS", "1")          # … headless wins
    monkeypatch.setattr(notifications.click, "confirm",
                        lambda *a, **k: pytest.fail("click.confirm must not be called"))
    assert notifications.confirm("x", default=False) is False
    assert notifications.confirm("x", default=True) is True
