"""— Stage 6 validator on by default + VALIDATOR_BLOCKED status."""
from __future__ import annotations

from unittest.mock import MagicMock


class TestConfigDefault:
    def test_config_default_is_true(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_SCROLL_VALIDATOR", raising=False)
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.scroll_validator is True

    def test_explicit_false_overrides(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "false")
        from sharing_on.config import Config
        c = Config.from_env()
        assert c.scroll_validator is False


class TestIsEnabled:
    def test_is_enabled_true_when_field_true(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_SCROLL_VALIDATOR", raising=False)
        from systemu.pipelines.scroll_validator import is_enabled
        cfg = MagicMock(scroll_validator=True, intelligent_supervisor_enabled=False)
        assert is_enabled(cfg) is True

    def test_is_enabled_false_when_field_false_and_supervisor_off(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_SCROLL_VALIDATOR", raising=False)
        from systemu.pipelines.scroll_validator import is_enabled
        cfg = MagicMock(scroll_validator=False, intelligent_supervisor_enabled=False)
        assert is_enabled(cfg) is False

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "false")
        from systemu.pipelines.scroll_validator import is_enabled
        cfg = MagicMock(scroll_validator=True, intelligent_supervisor_enabled=True)
        assert is_enabled(cfg) is False


class TestValidatorBlockedStatusEnum:
    def test_enum_value(self):
        from systemu.core.models import ScrollStatus
        assert ScrollStatus.VALIDATOR_BLOCKED.value == "validator_blocked"
