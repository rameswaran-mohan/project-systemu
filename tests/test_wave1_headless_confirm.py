"""Wave 1.1 — the destructive-action confirm gate must never block (or silently
diverge from notify_user) in non-interactive contexts.

Root cause: ``notifications.confirm`` only checked isatty()/SYSTEMU_HEADLESS,
while ``notify_user`` honoured SYSTEMU_NON_INTERACTIVE plus the Windows
unreliable-TTY signals — so the documented operator switch did not cover the
destructive gate. Both now share one ``is_headless()`` detector.
"""
import io

import pytest

from systemu.interface import notifications as notif


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("SYSTEMU_HEADLESS", "SYSTEMU_NON_INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)


def _fake_tty(monkeypatch, *, tty: bool):
    class _Stdin(io.StringIO):
        def isatty(self):
            return tty
    monkeypatch.setattr("sys.stdin", _Stdin())


class TestIsHeadless:
    def test_no_tty_is_headless(self, monkeypatch):
        _fake_tty(monkeypatch, tty=False)
        assert notif.is_headless() is True

    def test_non_interactive_env_is_headless_even_with_tty(self, monkeypatch):
        _fake_tty(monkeypatch, tty=True)
        monkeypatch.setenv("SYSTEMU_NON_INTERACTIVE", "true")
        assert notif.is_headless() is True

    def test_headless_env_is_headless_even_with_tty(self, monkeypatch):
        _fake_tty(monkeypatch, tty=True)
        monkeypatch.setenv("SYSTEMU_HEADLESS", "1")
        assert notif.is_headless() is True


class TestConfirmHeadless:
    def test_confirm_honours_non_interactive_env(self, monkeypatch):
        # THE BUG: with a live TTY + SYSTEMU_NON_INTERACTIVE=true, confirm()
        # used to fall through to click.confirm and block on stdin.
        _fake_tty(monkeypatch, tty=True)
        monkeypatch.setenv("SYSTEMU_NON_INTERACTIVE", "true")
        called = {"prompted": False}
        monkeypatch.setattr(
            notif.click, "confirm",
            lambda *a, **k: called.__setitem__("prompted", True) or True,
        )
        assert notif.confirm("destroy?", default=False) is False
        assert called["prompted"] is False, "must not prompt in non-interactive mode"

    def test_confirm_no_tty_returns_default(self, monkeypatch):
        _fake_tty(monkeypatch, tty=False)
        assert notif.confirm("destroy?", default=False) is False
        assert notif.confirm("proceed?", default=True) is True
