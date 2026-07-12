"""R-P3a — the router accumulator hook is LIVE (Phase A step 2).

These drive the REAL token-capture path in ``llm_router`` (only the network
client is mocked, exactly as the existing ``test_llm_router`` does). The hook is
NOT stubbed — if these pass, the accumulator is proven to read the real
per-call ``input_tokens``/``output_tokens`` and attribute them via the ambient
``execution_id``.

The load-bearing surprise this pins: the router runs sync calls in a
``ThreadPoolExecutor`` worker (``_run_coroutine``); ``concurrent.futures`` does
NOT copy contextvars, so a naive ``current_execution_id()`` read there would be
None and orphan EVERY call. ``_run_coroutine`` must propagate the calling
thread's context.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import systemu.core.llm_router as router
from systemu.core.llm_router import llm_call, llm_call_json
from systemu.runtime import costing
from systemu.runtime.chat_submission_ctx import set_execution_id
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
def _reset(monkeypatch):
    router._client = None
    costing.reset_ledger()
    monkeypatch.delenv(costing.PRICE_OVERRIDE_ENV, raising=False)
    yield
    router._client = None
    costing.reset_ledger()
    # Always clear the ambient execution_id so a leak can't cross tests.
    set_execution_id(None)


def _make_mock_client(mock_async_openai, *, prompt_tokens=10, completion_tokens=5,
                      content="hello"):
    mock_client = AsyncMock()
    mock_response = AsyncMock()
    mock_choice = AsyncMock()
    mock_choice.message.content = content
    mock_choice.message.reasoning_details = []
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = prompt_tokens
    mock_response.usage.completion_tokens = completion_tokens
    mock_client.chat.completions.create.return_value = mock_response
    ctx = AsyncMock()
    ctx.__aenter__.return_value = mock_client
    ctx.__aexit__.return_value = False
    mock_async_openai.return_value = ctx
    return mock_client


@pytest.mark.asyncio
@patch("systemu.core.llm_router.AsyncOpenAI")
async def test_async_call_records_real_tokens_under_the_ambient_eid(mock_openai, dummy_config):
    """The async path is awaited in-context: the hook reads the real captured
    tokens and attributes to the run set in this context."""
    _make_mock_client(mock_openai, prompt_tokens=17, completion_tokens=8)
    token = set_execution_id("run-async")
    try:
        result = await llm_call(tier=2, system="s", user="u", config=dummy_config)
    finally:
        set_execution_id(None, reset_token=token)

    assert result["input_tokens"] == 17 and result["output_tokens"] == 8
    rows = costing.usage_rows("run-async")
    assert len(rows) == 1
    assert rows[0]["model"] == "test/tier2"
    assert rows[0]["tokens_in"] == 17 and rows[0]["tokens_out"] == 8


@patch("systemu.core.llm_router.AsyncOpenAI")
def test_sync_llm_call_json_propagates_context_into_the_worker_thread(mock_openai, dummy_config):
    """THE surprise: llm_call_json hops into a ThreadPoolExecutor worker. Without
    context propagation the hook would orphan (None eid). With it, the call
    attributes to the run set in the CALLING thread."""
    _make_mock_client(mock_openai, prompt_tokens=21, completion_tokens=4,
                      content='{"ok": true}')
    token = set_execution_id("run-sync")
    try:
        out = llm_call_json(tier=1, system="s", user="u", config=dummy_config)
    finally:
        set_execution_id(None, reset_token=token)

    assert out == {"ok": True}
    rows = costing.usage_rows("run-sync")
    assert len(rows) == 1
    assert rows[0]["tokens_in"] == 21 and rows[0]["tokens_out"] == 4
    assert rows[0]["model"] == "test/tier1"


@patch("systemu.core.llm_router.AsyncOpenAI")
def test_call_outside_any_run_does_not_orphan(mock_openai, dummy_config):
    """No ambient execution_id → the hook is a no-op (no phantom orphan row)."""
    _make_mock_client(mock_openai, content='{"ok": true}')
    set_execution_id(None)
    llm_call_json(tier=1, system="s", user="u", config=dummy_config)
    # Nothing recorded anywhere.
    assert costing.ledger_run_ids() == []


@pytest.mark.asyncio
async def test_native_provider_path_also_records(dummy_config, monkeypatch):
    """The native (Anthropic/Ollama) path returns via _llm_call_via_provider —
    it must hook too, not just the OpenRouter path."""
    from systemu.llm.providers.base import LLMResponse

    async def _fake_call(self, **kwargs):
        return LLMResponse(content='{"ok": true}',
                           usage={"input": 33, "output": 12}, model="native/model")

    class _FakeProvider:
        call = _fake_call

    monkeypatch.setattr(router, "_uses_native_path", lambda tier, config: True)
    monkeypatch.setattr(router, "_get_provider", lambda config, tier: _FakeProvider())

    token = set_execution_id("run-native")
    try:
        await llm_call(tier=1, system="s", user="u", config=dummy_config)
    finally:
        set_execution_id(None, reset_token=token)

    rows = costing.usage_rows("run-native")
    assert len(rows) == 1
    assert rows[0]["tokens_in"] == 33 and rows[0]["tokens_out"] == 12
