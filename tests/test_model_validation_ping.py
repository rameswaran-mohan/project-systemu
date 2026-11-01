"""W14 S4 ping path: ``model_validation._ping`` -> ``llm_router._ping_model``.

``validate_model`` validates google / openai / anthropic models with a live
one-shot completion, because those providers have no catalog to list. It
delegated to ``llm_router._ping_model``, which did not exist: the import raised,
``_ping`` caught the ImportError, and EVERY such validation answered "the ping
helper is unavailable" -- a valid key and model were reported as failing. No
test ran that success path, so nothing noticed.

These tests drive ``validate_model`` end to end through the REAL router helper,
with only the provider's network call faked at the adapter.
"""
from __future__ import annotations

import pytest

from systemu.runtime import model_validation as mv
from systemu.llm.providers import LLMResponse
from systemu.llm.providers.google import GoogleProvider
from systemu.llm.providers.openai import OpenAIProvider


@pytest.mark.parametrize("provider,cls,model", [
    ("openai", OpenAIProvider, "gpt-4o-mini"),
    ("google", GoogleProvider, "gemini-2.5-flash"),
])
def test_validate_model_ping_success_path(monkeypatch, provider, cls, model):
    seen = {}

    async def _fake_call(self, *, messages, model, **kwargs):
        seen["model"] = model
        seen["kwargs"] = kwargs
        seen["cls"] = type(self)
        return LLMResponse(content="ok", model=model)

    monkeypatch.setattr(cls, "call", _fake_call)
    ok, why = mv.validate_model(provider=provider, model=model,
                                credential="test-credential")
    assert (ok, why) == (True, ""), why
    assert seen["model"] == model
    assert seen["cls"] is cls, "the ping must go through the provider the operator chose"
    assert seen["kwargs"].get("max_tokens", 0) <= 16, "a ping must stay tiny"


def test_validate_model_ping_failure_reports_and_scrubs_the_credential(monkeypatch):
    # DEC-31: the secret is longer than every cap on this path.
    secret = "s3cr3t-" + "x" * 600

    async def _fake_call(self, *, messages, model, **kwargs):
        raise RuntimeError(f"401 invalid key {secret} for {model}")

    monkeypatch.setattr(OpenAIProvider, "call", _fake_call)
    ok, why = mv.validate_model(provider="openai", model="gpt-4o-mini",
                                credential=secret)
    assert ok is False
    assert "ping failed" in why
    for i in range(0, len(secret) - 12, 12):
        assert secret[i:i + 12] not in why, "credential leaked into the reason"


def test_ping_model_never_raises_on_an_unknown_provider():
    from systemu.core.llm_router import _ping_model
    ok, why = _ping_model(provider="nonesuch", model="m", credential="k", timeout=1)
    assert ok is False and why
