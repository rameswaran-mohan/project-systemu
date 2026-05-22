"""dashboard renders a persistent security banner when
SYSTEMU_AUTO_FORGE_TOOLS=true."""


def test_autoforge_banner_message_when_enabled(monkeypatch):
    from systemu.interface.dashboard import _autoforge_banner_message
    monkeypatch.setenv("SYSTEMU_AUTO_FORGE_TOOLS", "true")
    msg = _autoforge_banner_message()
    assert msg is not None
    # Banner must clearly identify what's off
    lower = msg.lower()
    assert "auto" in lower and "forge" in lower
    # Must mention security or gate bypass so the operator understands risk
    assert "security" in lower or "gate" in lower or "bypass" in lower


def test_autoforge_banner_none_when_disabled(monkeypatch):
    from systemu.interface.dashboard import _autoforge_banner_message
    monkeypatch.setenv("SYSTEMU_AUTO_FORGE_TOOLS", "false")
    assert _autoforge_banner_message() is None


def test_autoforge_banner_none_when_unset(monkeypatch):
    from systemu.interface.dashboard import _autoforge_banner_message
    monkeypatch.delenv("SYSTEMU_AUTO_FORGE_TOOLS", raising=False)
    assert _autoforge_banner_message() is None


def test_autoforge_banner_case_insensitive_true(monkeypatch):
    """Various truthy spellings should all enable the banner."""
    from systemu.interface.dashboard import _autoforge_banner_message
    for value in ("true", "TRUE", "True"):
        monkeypatch.setenv("SYSTEMU_AUTO_FORGE_TOOLS", value)
        assert _autoforge_banner_message() is not None, \
            f"value {value!r} should enable the banner"
