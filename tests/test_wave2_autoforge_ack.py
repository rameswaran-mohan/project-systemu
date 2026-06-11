"""Wave 2.3 — AUTO_FORGE needs an explicit second factor.

One ``.env`` line (SYSTEMU_AUTO_FORGE_TOOLS=true) used to disable all three
tool security gates.  Now it ALSO requires
``SYSTEMU_AUTO_FORGE_ACK=I_UNDERSTAND_ALL_GATES_ARE_BYPASSED`` — without the
ack, auto-forge stays OFF and a loud warning explains what's missing.
"""
import pytest

from sharing_on.config import _load_auto_forge_tools, AUTO_FORGE_ACK_PHRASE


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SYSTEMU_AUTO_FORGE_TOOLS", raising=False)
    monkeypatch.delenv("SYSTEMU_AUTO_FORGE_ACK", raising=False)


class TestAutoForgeAck:
    def test_off_by_default(self):
        assert _load_auto_forge_tools() is False

    def test_flag_alone_is_not_enough(self, monkeypatch, capsys):
        monkeypatch.setenv("SYSTEMU_AUTO_FORGE_TOOLS", "true")
        assert _load_auto_forge_tools() is False
        err = capsys.readouterr().err
        assert "SYSTEMU_AUTO_FORGE_ACK" in err   # tells the operator what's missing

    def test_flag_plus_ack_enables(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_AUTO_FORGE_TOOLS", "true")
        monkeypatch.setenv("SYSTEMU_AUTO_FORGE_ACK", AUTO_FORGE_ACK_PHRASE)
        assert _load_auto_forge_tools() is True

    def test_wrong_ack_phrase_stays_off(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_AUTO_FORGE_TOOLS", "true")
        monkeypatch.setenv("SYSTEMU_AUTO_FORGE_ACK", "yes")
        assert _load_auto_forge_tools() is False

    def test_ack_alone_does_nothing(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_AUTO_FORGE_ACK", AUTO_FORGE_ACK_PHRASE)
        assert _load_auto_forge_tools() is False
