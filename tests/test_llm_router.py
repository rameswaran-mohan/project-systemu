import pytest
from unittest.mock import AsyncMock, patch
from systemu.core.llm_router import llm_call, llm_call_json
import systemu.core.llm_router
from sharing_on.config import Config

@pytest.fixture
def dummy_config():
    config = Config.from_env()
    config.openrouter_api_key = "dummy_key"
    config.tier1_model = "test/tier1"
    config.tier2_model = "test/tier2"
    config.tier3_model = "test/tier3"
    return config

@pytest.fixture(autouse=True)
def reset_router_client():
    systemu.core.llm_router._client = None
    yield
    systemu.core.llm_router._client = None

def _make_mock_client(content: str, mock_async_openai):
    """Wire up AsyncOpenAI mock as an async context manager returning content."""
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_choice = AsyncMock()
    mock_choice.message.content = content
    mock_choice.message.reasoning_details = []
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    mock_client.chat.completions.create.return_value = mock_response

    # AsyncOpenAI is used as: async with _get_client() as client: ...
    # so __aenter__ must return the mock_client
    ctx = AsyncMock()
    ctx.__aenter__.return_value = mock_client
    ctx.__aexit__.return_value = False
    mock_async_openai.return_value = ctx
    return mock_client


@pytest.mark.asyncio
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_llm_call_tier_dispatch(mock_async_openai, dummy_config):
    mock_client = _make_mock_client("Test content", mock_async_openai)

    result = await llm_call(
        tier=1,
        system="System",
        user="User",
        config=dummy_config,
    )

    assert result["model"] == "test/tier1"
    assert result["content"] == "Test content"
    mock_client.chat.completions.create.assert_called_once()
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "test/tier1"


@patch("systemu.core.llm_router.AsyncOpenAI")
def test_llm_call_json_parsing(mock_async_openai, dummy_config):
    _make_mock_client('{"key": "value"}', mock_async_openai)

    # llm_call_json is the synchronous wrapper — do NOT await it
    result = llm_call_json(
        tier=2,
        system="System",
        user="User",
        config=dummy_config,
    )

    assert result == {"key": "value"}
