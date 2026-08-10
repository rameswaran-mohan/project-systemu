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
    """F8 reworded this banner: "no credential set" was wrong for Ollama, the
    one KEYLESS provider — it has no credential to be missing, so the banner
    now says "not usable" and is decided by the single satisfaction mint. The
    behaviour this test names is asserted directly instead of grepped, since
    the grep is what let the Ollama row stay green for so long."""
    from sharing_on.config import Config
    from systemu.runtime.provider_status import (all_provider_statuses,
                                                 unusable_selected)

    src = inspect.getsource(__import__("systemu.interface.pages.settings",
                                       fromlist=["x"]))
    assert "Selected for a tier but not usable" in src
    assert "s-banner--danger" in src

    cfg = Config(openrouter_api_key="", google_api_key="",
                 anthropic_api_key="", openai_api_key="",
                 tier2_provider="anthropic")
    quiet = lambda _u, _t: ("unknown", "not probed")  # noqa: E731
    flagged = unusable_selected(all_provider_statuses(cfg, probe=quiet),
                                [cfg.tier1_provider, cfg.tier2_provider,
                                 cfg.tier3_provider])
    assert [f.provider for f in flagged] == ["anthropic"], flagged
    assert "ANTHROPIC_API_KEY" in flagged[0].detail
