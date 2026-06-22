"""W14 S6 — per-tier provider + credential setup loop (operator: ask each
tier individually; reuse a credential when the same provider repeats)."""
from __future__ import annotations

from sharing_on.setup_flow import _parse_env, run_setup


def test_per_tier_providers_and_reuse(tmp_path):
    p = tmp_path / ".env"
    run_setup(
        interactive=False, env_path=p, validate=False,
        tier_specs=[
            {"provider": "anthropic", "model": "claude-sonnet-4.5", "credential": "sk-ant"},
            {"provider": "anthropic", "model": "claude-haiku", "credential": "sk-ant"},
            {"provider": "ollama", "model": "ollama/llama3.1", "credential": "http://localhost:11434"},
        ])
    env = _parse_env(p.read_text(encoding="utf-8"))
    assert env["SYSTEMU_TIER1_PROVIDER"] == "anthropic"
    assert env["SYSTEMU_TIER2_PROVIDER"] == "anthropic"
    assert env["SYSTEMU_TIER3_PROVIDER"] == "ollama"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant"          # collected once, shared
    assert env["OLLAMA_URL"] == "http://localhost:11434"
    assert env["SYSTEMU_TIER1_MODEL"] == "claude-sonnet-4.5"
    assert env["SYSTEMU_TIER3_MODEL"] == "ollama/llama3.1"


def test_different_providers_each_get_their_key(tmp_path):
    p = tmp_path / ".env"
    run_setup(
        interactive=False, env_path=p, validate=False,
        tier_specs=[
            {"provider": "openai", "model": "gpt-5", "credential": "sk-oai"},
            {"provider": "google", "model": "gemini-3-flash-preview", "credential": "g-key"},
            {"provider": "auto", "model": "deepseek/deepseek-v4-flash", "credential": ""},
        ])
    env = _parse_env(p.read_text(encoding="utf-8"))
    assert env["OPENAI_API_KEY"] == "sk-oai"
    assert env["GOOGLE_API_KEY"] == "g-key"
    # auto tier writes no provider key, and "auto" is not written as a provider
    assert "SYSTEMU_TIER3_PROVIDER" not in env


def test_openrouter_simple_path_still_works(tmp_path):
    p = tmp_path / ".env"
    run_setup(interactive=False, key="sk-or", preset="balanced",
              env_path=p, validate=False)
    env = _parse_env(p.read_text(encoding="utf-8"))
    assert env["OPENROUTER_API_KEY"] == "sk-or"
    assert env["SYSTEMU_MODEL_PRESET"] == "balanced"


def test_anthropic_available_returns_bool():
    from sharing_on.setup_flow import anthropic_available
    assert isinstance(anthropic_available(), bool)


def test_cli_exposes_per_tier_flags():
    from click.testing import CliRunner
    from sharing_on.cli import cli
    out = CliRunner().invoke(cli, ["setup", "--help"]).output
    for flag in ("--tier1-provider", "--tier2-provider", "--tier3-provider",
                 "--anthropic-key", "--openai-key", "--ollama-url"):
        assert flag in out, flag


def test_cli_tier_flags_write_env(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from sharing_on.cli import cli
    from sharing_on.setup_flow import _parse_env
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(cli, [
        "setup", "--no-validate",
        "--tier1-provider", "openai", "--tier1-model", "gpt-5", "--openai-key", "sk-oai",
        "--tier2-provider", "auto", "--tier3-provider", "ollama",
        "--tier3-model", "ollama/llama3.1", "--ollama-url", "http://localhost:11434"])
    assert res.exit_code == 0, res.output
    env = _parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert env["SYSTEMU_TIER1_PROVIDER"] == "openai"
    assert env["OPENAI_API_KEY"] == "sk-oai"
    assert env["OLLAMA_URL"] == "http://localhost:11434"
