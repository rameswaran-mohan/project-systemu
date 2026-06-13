"""W14 S2 — the router resolves credentials from Config, not os.environ,
and the OpenAI branch never leaks the OpenRouter key to api.openai.com."""
from __future__ import annotations

import inspect

from systemu.core import llm_router


def test_keyaware_native_map_uses_config():
    # the keyaware map reads config defensively (getattr — it's also called
    # with SimpleNamespace), NOT os.environ.
    src = inspect.getsource(llm_router._resolve_provider_keyaware)
    assert '"anthropic_api_key"' in src and '"openai_api_key"' in src
    assert "environ" not in src, "native-key map must not read os.environ"


def test_get_provider_reads_config_creds():
    src = inspect.getsource(llm_router._get_provider)
    assert "config.anthropic_api_key" in src
    assert "config.openai_api_key" in src
    assert "config.ollama_url" in src
    assert "environ.get" not in src, "no ad-hoc os.environ reads remain"


def test_no_openai_to_openrouter_key_leak():
    src = inspect.getsource(llm_router._get_client)
    # the OpenAI branch must use the dedicated key, never the openrouter key
    assert 'environ.get("OPENAI_API_KEY", config.openrouter_api_key)' not in src
    assert "config.openai_api_key" in src
