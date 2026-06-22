"""W14 S8 — edge hardening: anthropic-extra boundary check, provider/model
mismatch (seeded in S4), and the Settings selected-provider-no-key red-flag."""
from __future__ import annotations

import inspect

from systemu.runtime import model_validation as mv


def test_provider_model_mismatch_flagged():
    ok, why = mv.validate_model(provider="openai",
                                model="deepseek/deepseek-v4-flash", credential="sk-x")
    assert not ok and "mismatch" in why.lower()


def test_anthropic_without_extra_is_config_error(monkeypatch):
    monkeypatch.setattr(mv, "_anthropic_importable", lambda: False)
    ok, why = mv.validate_model(provider="anthropic",
                                model="claude-sonnet-4.5", credential="sk-ant")
    assert not ok and "anthropic" in why.lower() and "install" in why.lower()


def test_anthropic_available_helper_is_bool():
    from sharing_on.setup_flow import anthropic_available
    assert isinstance(anthropic_available(), bool)


def test_settings_red_flags_selected_provider_without_key():
    src = inspect.getsource(__import__("systemu.interface.pages.settings",
                                       fromlist=["x"]))
    assert "Selected for a tier but no credential set" in src
    assert "s-banner--danger" in src
