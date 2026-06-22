import pytest


def test_registry_auto_routes_by_model_name():
    from systemu.llm.providers import resolve_provider_class
    from systemu.llm.providers.openrouter import OpenRouterProvider
    from systemu.llm.providers.google import GoogleProvider
    from systemu.llm.providers.openai import OpenAIProvider
    from systemu.llm.providers.ollama import OllamaProvider

    assert resolve_provider_class("deepseek/deepseek-v4-flash") is OpenRouterProvider
    assert resolve_provider_class("z-ai/glm-4.5-air:free") is OpenRouterProvider
    assert resolve_provider_class("gemini-3.1-flash-lite-preview") is GoogleProvider
    assert resolve_provider_class("google/gemini-2.5-pro") is GoogleProvider
    assert resolve_provider_class("gpt-4o") is OpenAIProvider
    assert resolve_provider_class("o3-mini") is OpenAIProvider
    assert resolve_provider_class("ollama/llama-3.3") is OllamaProvider


def test_registry_routes_claude_to_anthropic_when_installed():
    pytest.importorskip("anthropic")
    from systemu.llm.providers import resolve_provider_class
    from systemu.llm.providers.anthropic import AnthropicProvider
    assert resolve_provider_class("claude-opus-4-7") is AnthropicProvider
    assert resolve_provider_class("anthropic/claude-haiku") is AnthropicProvider


def test_registry_env_override_wins(monkeypatch):
    pytest.importorskip("anthropic")
    from systemu.llm.providers import resolve_provider_class
    from systemu.llm.providers.anthropic import AnthropicProvider
    # Force anthropic for a deepseek model: env override beats matches()
    cls = resolve_provider_class("deepseek/deepseek-v4-flash",
                                  override_name="anthropic")
    assert cls is AnthropicProvider


def test_registry_unknown_override_raises():
    from systemu.llm.providers import resolve_provider_class
    with pytest.raises(ValueError, match="unknown provider"):
        resolve_provider_class("x", override_name="bogus")


def test_registry_fallback_when_anthropic_unavailable(monkeypatch):
    """If a claude model is queried but the anthropic SDK isn't available,
    fall back to OpenRouter (which can also serve claude via its catalog)."""
    from systemu.llm import providers as pr
    monkeypatch.setattr(pr, "_ANTHROPIC_AVAILABLE", False)
    monkeypatch.setattr(pr, "AnthropicProvider", None)
    # Need to rebuild the registry without anthropic
    from systemu.llm.providers.openrouter import OpenRouterProvider
    from systemu.llm.providers.google import GoogleProvider
    from systemu.llm.providers.openai import OpenAIProvider
    from systemu.llm.providers.ollama import OllamaProvider
    monkeypatch.setattr(pr, "_REGISTRY",
                        [GoogleProvider, OpenAIProvider, OllamaProvider, OpenRouterProvider])
    # claude-opus-4-7 isn't claimed by any of the remaining providers,
    # so resolve_provider_class falls through to the catch-all (OpenRouter).
    # But OpenRouter.matches("claude-opus-4-7") returns False (it's claimed).
    # The catch-all default at end of resolve_provider_class still returns OpenRouterProvider.
    cls = pr.resolve_provider_class("claude-opus-4-7")
    assert cls is OpenRouterProvider
