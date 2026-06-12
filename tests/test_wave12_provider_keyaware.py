"""W12 — key-aware provider resolution (audit F3, parallel-session fix).

Auto-detection routes ``google/*`` / ``anthropic/*`` / ``gpt-*`` model ids
to native providers — but OpenRouter legitimately serves those same
catalog ids. With only an OpenRouter key configured, picking
``google/gemini-3-flash-preview`` as a tier model failed with a cryptic
400 ("Missing or invalid Authorization header"). The router now falls
back to OpenRouter when the native key is absent; an explicit
``SYSTEMU_TIER{N}_PROVIDER`` override always wins.

Field-proven: the whole A2 recording E2E ran gemini-3-flash-preview as
tier 1 through this code via OpenRouter.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def _config(openrouter="", google=""):
    return SimpleNamespace(openrouter_api_key=openrouter,
                           google_api_key=google)


class TestKeyAwareResolution:
    def test_google_model_without_google_key_falls_back_to_openrouter(
            self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        from systemu.core.llm_router import _resolve_provider_keyaware
        from systemu.llm.providers.openrouter import OpenRouterProvider
        cls = _resolve_provider_keyaware(
            "google/gemini-3-flash-preview", "", _config(openrouter="sk-or-x"))
        assert cls is OpenRouterProvider

    def test_google_model_with_google_key_stays_native(self):
        from systemu.core.llm_router import _resolve_provider_keyaware
        cls = _resolve_provider_keyaware(
            "google/gemini-3-flash-preview", "",
            _config(openrouter="sk-or-x", google="g-key"))
        assert cls.__name__ == "GoogleProvider"

    def test_explicit_override_always_wins(self):
        from systemu.core.llm_router import _resolve_provider_keyaware
        cls = _resolve_provider_keyaware(
            "google/gemini-3-flash-preview", "google",
            _config(openrouter="sk-or-x"))
        assert cls.__name__ == "GoogleProvider", \
            "an explicit SYSTEMU_TIER*_PROVIDER must never be second-guessed"

    def test_no_openrouter_key_keeps_native_class(self, monkeypatch):
        """With neither key, behavior is unchanged (native class + its own
        error) — the fallback only fires when OpenRouter CAN serve."""
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        from systemu.core.llm_router import _resolve_provider_keyaware
        cls = _resolve_provider_keyaware(
            "google/gemini-3-flash-preview", "", _config())
        assert cls.__name__ == "GoogleProvider"

    def test_openrouter_models_unaffected(self):
        from systemu.core.llm_router import _resolve_provider_keyaware
        from systemu.llm.providers.openrouter import OpenRouterProvider
        cls = _resolve_provider_keyaware(
            "deepseek/deepseek-v4-flash", "", _config(openrouter="sk-or-x"))
        assert cls is OpenRouterProvider
