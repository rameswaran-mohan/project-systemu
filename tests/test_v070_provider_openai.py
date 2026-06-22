def test_openai_matches_gpt_models():
    from systemu.llm.providers.openai import OpenAIProvider
    assert OpenAIProvider.matches("gpt-4o") is True
    assert OpenAIProvider.matches("gpt-5") is True
    assert OpenAIProvider.matches("openai/o3-mini") is True
    assert OpenAIProvider.matches("o1-preview") is True
    assert OpenAIProvider.matches("o3-mini-2026-01") is True


def test_openai_rejects_non_gpt():
    from systemu.llm.providers.openai import OpenAIProvider
    assert OpenAIProvider.matches("claude-opus-4-7") is False
    assert OpenAIProvider.matches("deepseek/deepseek-v4-flash") is False
    assert OpenAIProvider.matches("gemini-3.1-flash-lite-preview") is False
    assert OpenAIProvider.matches("ollama/llama-3.3") is False
