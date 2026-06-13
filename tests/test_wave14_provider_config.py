"""W14 S1 — provider credentials as first-class Config fields.

The router read anthropic/openai/ollama creds ad-hoc from os.environ, so the
Settings UI (which reads config.*) could never see or edit them. Making them
real Config fields is the keystone for the whole provider-selection wave.
Zero behavior change: defaults reproduce today.
"""
from __future__ import annotations

from sharing_on.config import Config


class TestProviderConfigFields:
    def test_new_fields_default_empty_or_standard(self, monkeypatch):
        for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OLLAMA_URL",
                  "OPENROUTER_BASE_URL"):
            monkeypatch.delenv(v, raising=False)
        c = Config.from_env()
        assert c.anthropic_api_key == ""
        assert c.openai_api_key == ""
        assert c.ollama_url == "http://localhost:11434"
        assert c.openrouter_base_url == "https://openrouter.ai/api/v1"

    def test_from_env_binds_new_vars(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-x")
        monkeypatch.setenv("OLLAMA_URL", "http://host:9999")
        monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy/api/v1")
        c = Config.from_env()
        assert c.anthropic_api_key == "sk-ant-x"
        assert c.openai_api_key == "sk-oai-x"
        assert c.ollama_url == "http://host:9999"
        assert c.openrouter_base_url == "https://proxy/api/v1"
