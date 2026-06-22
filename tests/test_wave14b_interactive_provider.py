"""W14b — the INTERACTIVE setup wizard asks which provider first (the gap:
`sharing_on setup` only ever asked for an OpenRouter key)."""
from __future__ import annotations

from sharing_on.setup_flow import _parse_env, run_setup


def _run(tmp_path, inputs, getpass_value="sk-secret"):
    p = tmp_path / ".env"
    it = iter(inputs)
    run_setup(interactive=True, env_path=p, validate=False,
              getpass_fn=lambda prompt: getpass_value,
              input_fn=lambda prompt: next(it, ""),
              print_fn=lambda s: None)
    return _parse_env(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_choice_openrouter_uses_simple_key_path(tmp_path):
    # choice 1 (OpenRouter) → falls through to the one-key+preset flow;
    # getpass supplies the key, then preset prompt "1", then output dir.
    env = _run(tmp_path, inputs=["1", "1", str(tmp_path / "out")],
               getpass_value="sk-or-key")
    assert env.get("OPENROUTER_API_KEY") == "sk-or-key"
    # simple path stores a preset name, not per-tier providers
    assert "SYSTEMU_TIER1_PROVIDER" not in env


def test_choice_single_anthropic_applies_to_all_tiers(tmp_path):
    # choice 4 (Anthropic) → one key for all three tiers, then model id.
    env = _run(tmp_path, inputs=["4", "claude-sonnet-4.5"],
               getpass_value="sk-ant-key")
    assert env["SYSTEMU_TIER1_PROVIDER"] == "anthropic"
    assert env["SYSTEMU_TIER2_PROVIDER"] == "anthropic"
    assert env["SYSTEMU_TIER3_PROVIDER"] == "anthropic"
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-key"   # one key, all tiers
    assert env["SYSTEMU_TIER1_MODEL"] == "claude-sonnet-4.5"


def test_choice_ollama_collects_base_url_not_key(tmp_path):
    # choice 5 (Ollama): the credential prompt is the base URL (input, not getpass)
    env = _run(tmp_path, inputs=["5", "http://localhost:11434", "ollama/llama3.1"])
    assert env["SYSTEMU_TIER1_PROVIDER"] == "ollama"
    assert env["OLLAMA_URL"] == "http://localhost:11434"


def test_per_tier_mix_with_reuse(tmp_path):
    # choice 6: tier1 openai (key) + model, tier2 "same as tier 1" (reuse),
    # tier3 ollama (url) + model.
    inputs = [
        "6",                       # advanced / per tier
        "3", "openai/gpt-4o",      # tier1: openai, model
        "s", "openai/gpt-4o-mini", # tier2: same as tier1 (reuse key), model
        "5", "http://localhost:11434", "ollama/llama3.1",  # tier3: ollama, url, model
    ]
    env = _run(tmp_path, inputs=inputs, getpass_value="sk-oai")
    assert env["SYSTEMU_TIER1_PROVIDER"] == "openai"
    assert env["SYSTEMU_TIER2_PROVIDER"] == "openai"   # same as tier 1
    assert env["SYSTEMU_TIER3_PROVIDER"] == "ollama"
    assert env["OPENAI_API_KEY"] == "sk-oai"           # collected once, reused
    assert env["OLLAMA_URL"] == "http://localhost:11434"
