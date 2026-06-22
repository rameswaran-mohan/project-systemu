def test_google_matches_gemini():
    from systemu.llm.providers.google import GoogleProvider
    assert GoogleProvider.matches("gemini-3.1-flash-lite-preview") is True
    assert GoogleProvider.matches("google/gemini-2.5-pro") is True
    assert GoogleProvider.matches("GEMINI-2-PRO") is True  # case-insensitive


def test_google_does_not_match_others():
    from systemu.llm.providers.google import GoogleProvider
    assert GoogleProvider.matches("deepseek/deepseek-v4-flash") is False
    assert GoogleProvider.matches("claude-opus-4-7") is False
    assert GoogleProvider.matches("gpt-4o") is False
