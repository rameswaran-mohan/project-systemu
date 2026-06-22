def test_ollama_matches_ollama_prefix():
    from systemu.llm.providers.ollama import OllamaProvider
    assert OllamaProvider.matches("ollama/llama-3.3") is True
    assert OllamaProvider.matches("ollama/qwen2.5") is True
    assert OllamaProvider.matches("OLLAMA/llama") is True  # case-insensitive


def test_ollama_rejects_non_ollama():
    from systemu.llm.providers.ollama import OllamaProvider
    assert OllamaProvider.matches("claude-opus-4-7") is False
    assert OllamaProvider.matches("gpt-4o") is False
    assert OllamaProvider.matches("gemini-3.1") is False
    assert OllamaProvider.matches("deepseek/deepseek-v4-flash") is False


def test_ollama_call_hits_local_endpoint(monkeypatch):
    """Mock httpx so we don't need a live Ollama daemon."""
    from systemu.llm.providers.ollama import OllamaProvider
    from unittest.mock import AsyncMock, MagicMock
    import asyncio

    p = OllamaProvider(base_url="http://localhost:11434")

    fake_resp = MagicMock()
    fake_resp.json = MagicMock(return_value={
        "model": "llama-3.3",
        "message": {"content": "hi"},
        "prompt_eval_count": 3,
        "eval_count": 1,
    })
    fake_resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(
        "systemu.llm.providers.ollama.httpx.AsyncClient",
        MagicMock(return_value=fake_client),
    )

    resp = asyncio.new_event_loop().run_until_complete(
        p.call(messages=[{"role": "user", "content": "hi"}],
               model="ollama/llama-3.3")
    )
    assert resp.content == "hi"
    assert resp.model == "llama-3.3"
    assert resp.usage == {"input": 3, "output": 1}

    # Confirm we hit /api/chat with the ollama/ prefix stripped from the model
    call_args = fake_client.post.call_args
    assert "/api/chat" in call_args.args[0]
    sent_json = call_args.kwargs["json"]
    assert sent_json["model"] == "llama-3.3"  # prefix stripped
    assert sent_json["stream"] is False
