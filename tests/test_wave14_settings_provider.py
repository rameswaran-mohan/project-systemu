"""W14 S7 — Settings exposes per-tier provider dropdowns + credential status,
keeping the keys-never-in-the-browser stance."""
from __future__ import annotations

import inspect

from systemu.interface.pages import settings


def test_per_tier_provider_dropdown_and_save():
    src = inspect.getsource(settings)
    assert "_provider_select(" in src and "ui.select" in src
    for env in ("SYSTEMU_TIER1_PROVIDER", "SYSTEMU_TIER2_PROVIDER",
                "SYSTEMU_TIER3_PROVIDER"):
        assert env in src, f"_save must write {env}"


def test_same_provider_master_toggle():
    src = inspect.getsource(settings)
    assert "Same provider for all tiers" in src


def test_provider_credential_status_rows():
    src = inspect.getsource(settings)
    for env in ("OPENROUTER_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY",
                "OPENAI_API_KEY", "OLLAMA_URL"):
        assert env in src


def test_keys_stay_readonly_no_password_inputs():
    src = inspect.getsource(settings)
    assert "type=password" not in src


def test_provider_options_include_all_five_plus_auto():
    assert set(settings._PROVIDER_OPTIONS) == {
        "", "openrouter", "google", "anthropic", "openai", "ollama"}
