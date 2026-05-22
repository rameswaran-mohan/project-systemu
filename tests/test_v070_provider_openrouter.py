def test_openrouter_matches_default_models():
    from systemu.llm.providers.openrouter import OpenRouterProvider
    assert OpenRouterProvider.matches("deepseek/deepseek-v4-flash") is True
    assert OpenRouterProvider.matches("z-ai/glm-4.5-air:free") is True


def test_openrouter_does_not_match_explicit_providers():
    """Other providers claim their prefixes; OpenRouter is the catch-all
    for everything that isn't already claimed."""
    from systemu.llm.providers.openrouter import OpenRouterProvider
    assert OpenRouterProvider.matches("gemini-3.1-flash-lite-preview") is False
    assert OpenRouterProvider.matches("google/gemini-2.5-pro") is False
    assert OpenRouterProvider.matches("claude-opus-4-7") is False
    assert OpenRouterProvider.matches("anthropic/claude-haiku") is False
    assert OpenRouterProvider.matches("gpt-4o") is False
    assert OpenRouterProvider.matches("openai/o3-mini") is False
    assert OpenRouterProvider.matches("ollama/llama-3.3") is False


def test_openrouter_matches_unknown_model_as_fallback():
    from systemu.llm.providers.openrouter import OpenRouterProvider
    assert OpenRouterProvider.matches("custom-org/some-model-9000") is True
