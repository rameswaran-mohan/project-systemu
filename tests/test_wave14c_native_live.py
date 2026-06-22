"""W14c (Task 9) — Anthropic/Ollama actually RUN on the live path.

The live `llm_call` used the AsyncOpenAI client only (OpenRouter/Google/
OpenAI shape). Native-shape providers (Anthropic SDK, Ollama httpx) now
route through the unified _get_provider().call() -> LLMResponse path. The
OpenAI-shape path is untouched (zero parity risk).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from systemu.core import llm_router
from systemu.llm.providers.base import LLMResponse


def _cfg(**kw):
    base = dict(openrouter_api_key="sk-or",
                openrouter_base_url="https://openrouter.ai/api/v1",
                google_api_key="", anthropic_api_key="sk-ant",
                openai_api_key="", ollama_url="http://localhost:11434",
                tier1_model="ollama/llama3.1", tier2_model="x", tier3_model="x",
                tier1_provider="ollama", tier2_provider="", tier3_provider="")
    base.update(kw)
    return SimpleNamespace(**base)


class TestNativePathDecision:
    def test_ollama_uses_native_path(self):
        assert llm_router._uses_native_path(1, _cfg(tier1_provider="ollama")) is True

    def test_openai_shape_providers_do_not(self):
        for prov in ("", "openrouter", "google", "openai"):
            cfg = _cfg(tier1_provider=prov,
                       tier1_model="deepseek/deepseek-v4-flash")
            assert llm_router._uses_native_path(1, cfg) is False, prov

    def test_anthropic_uses_native_when_available(self):
        pytest.importorskip("anthropic")
        cfg = _cfg(tier1_provider="anthropic",
                   tier1_model="anthropic/claude-sonnet-4.5")
        assert llm_router._uses_native_path(1, cfg) is True


class TestNativeCallRuns:
    def _fake_provider(self, response: LLMResponse):
        class _P:
            async def call(self, *, messages, model, **kwargs):
                return response
        return _P()

    def test_ollama_call_normalizes_response(self, monkeypatch):
        resp = LLMResponse(content="hello from llama", model="llama3.1",
                           usage={"input": 7, "output": 11})
        monkeypatch.setattr(llm_router, "_get_provider",
                            lambda config, tier: self._fake_provider(resp))
        out = asyncio.run(llm_router.llm_call(1, "sys", "usr", _cfg()))
        assert out["content"] == "hello from llama"
        assert out["input_tokens"] == 7 and out["output_tokens"] == 11
        assert out["model"] == "ollama/llama3.1" and out["tier"] == 1

    def test_native_json_mode_extracts_dict(self, monkeypatch):
        resp = LLMResponse(content='Here: {"action": "ANSWER", "answer_md": "ok"}',
                           model="llama3.1", usage={"input": 1, "output": 2})
        monkeypatch.setattr(llm_router, "_get_provider",
                            lambda config, tier: self._fake_provider(resp))
        out = asyncio.run(llm_router.llm_call(
            1, "sys", "usr", _cfg(), response_format={"type": "json_object"}))
        assert isinstance(out["content"], dict)
        assert out["content"]["action"] == "ANSWER"

    def test_native_unvalidated_invalid_model_blocks(self, monkeypatch):
        class _P:
            async def call(self, *, messages, model, **kwargs):
                raise RuntimeError("404 - model not a valid model ID")
        monkeypatch.setattr(llm_router, "_get_provider",
                            lambda config, tier: _P())
        # nothing validated → config error, not a silent cross-provider degrade
        with pytest.raises(RuntimeError) as ei:
            asyncio.run(llm_router.llm_call(1, "sys", "usr", _cfg()))
        msg = str(ei.value).lower()
        assert "config" in msg or "never validated" in msg or "fix" in msg
