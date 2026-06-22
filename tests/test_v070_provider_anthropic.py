import pytest


def test_anthropic_matches_claude_models():
    pytest.importorskip("anthropic")
    from systemu.llm.providers.anthropic import AnthropicProvider
    assert AnthropicProvider.matches("claude-opus-4-7") is True
    assert AnthropicProvider.matches("claude-sonnet-4-7") is True
    assert AnthropicProvider.matches("anthropic/claude-haiku") is True


def test_anthropic_rejects_non_claude():
    pytest.importorskip("anthropic")
    from systemu.llm.providers.anthropic import AnthropicProvider
    assert AnthropicProvider.matches("gpt-4o") is False
    assert AnthropicProvider.matches("deepseek/deepseek-v4-flash") is False
    assert AnthropicProvider.matches("gemini-3.1-flash-lite-preview") is False


def test_anthropic_provider_bridges_openai_messages():
    """The Anthropic API splits 'system' off from 'messages'. Our provider
    must extract the system message from OpenAI-shape messages and pass it
    via Anthropic's separate `system` kwarg."""
    pytest.importorskip("anthropic")
    from systemu.llm.providers.anthropic import AnthropicProvider
    from unittest.mock import AsyncMock, MagicMock
    import asyncio

    p = AnthropicProvider.__new__(AnthropicProvider)  # bypass __init__
    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(text="hello world")]
    fake_msg.model = "claude-opus-4-7"
    fake_msg.usage = MagicMock(input_tokens=5, output_tokens=2)
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_msg)
    p._client = fake_client

    resp = asyncio.new_event_loop().run_until_complete(
        p.call(
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "say hi"},
            ],
            model="claude-opus-4-7",
        )
    )
    assert resp.content == "hello world"
    assert resp.model == "claude-opus-4-7"
    assert resp.usage == {"input": 5, "output": 2}

    # Confirm we extracted system + rest
    kwargs = fake_client.messages.create.call_args.kwargs
    assert kwargs["system"] == "be brief"
    assert kwargs["messages"] == [{"role": "user", "content": "say hi"}]
    # max_tokens must be passed (Anthropic requires it)
    assert "max_tokens" in kwargs
