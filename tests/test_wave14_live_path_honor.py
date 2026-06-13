"""W14 S3 — the live AsyncOpenAI path refuses native-Anthropic/Ollama
instead of silently degrading them to an OpenRouter client."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from systemu.core import llm_router


def _cfg(**kw):
    base = dict(openrouter_api_key="sk-or",
                openrouter_base_url="https://openrouter.ai/api/v1",
                google_api_key="", anthropic_api_key="sk-ant",
                openai_api_key="", ollama_url="http://localhost:11434",
                tier1_model="x", tier2_model="x", tier3_model="x",
                tier1_provider="", tier2_provider="", tier3_provider="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_forced_anthropic_refuses_not_degrades():
    with pytest.raises(Exception) as ei:
        llm_router._get_client(
            _cfg(tier1_provider="anthropic",
                 tier1_model="anthropic/claude-sonnet-4.5"), 1)
    assert "anthropic" in str(ei.value).lower()


def test_forced_ollama_refuses():
    with pytest.raises(Exception) as ei:
        llm_router._get_client(
            _cfg(tier1_provider="ollama", tier1_model="ollama/llama3.1"), 1)
    assert "ollama" in str(ei.value).lower()


def test_openrouter_path_still_returns_a_client():
    c = llm_router._get_client(
        _cfg(tier1_provider="", tier1_model="deepseek/deepseek-v4-flash"), 1)
    assert c is not None
