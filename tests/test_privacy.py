"""R-P3b — the "What leaves this machine" privacy report (§15.7 honest interim rule).

Pins AC6 (truthful pre/post-S2) AND the truthfulness-audit fixes: per-tier locality
(a local tier-1 with a remote tier still "leaves"), the real network destination
(openrouter.ai, not the model vendor), the plaintext-fallback flag (the value the
runtime actually emits), the named default third-party endpoints, and no leaked key.
"""
from __future__ import annotations

from systemu.runtime import privacy

_LOCAL = "ollama/llama3"
_REMOTE = "deepseek/deepseek-v4-flash"          # routes through openrouter.ai


def _profile(**over):
    base = dict(os="win32", os_family="windows", arch="x86_64",
                python_version="3.12.0", capture_available=True,
                keyring_backend="dpapi", forged_net_jail="absent",
                docker_mode=False, provider_configured=True, host_capabilities=[])
    base.update(over)
    return base


def _tiers(t1=_REMOTE, t2=_REMOTE, t3=_REMOTE):
    return {"tier1": t1, "tier2": t2, "tier3": t3}


def _section(report, key):
    return next(s for s in report["sections"] if s["key"] == key)


# ── AC6: truthful pre- AND post-S2 ───────────────────────────────────────────

def test_ac6_pre_s2_outbound_is_unsandboxed_honestly():
    r = privacy.privacy_report(profile=_profile(forged_net_jail="absent"), tier_models=_tiers())
    s = _section(r, "sandbox")
    assert s["status"] == "unsandboxed"
    assert "no os-level egress jail" in s["detail"].lower() and "hard-deny" in s["detail"].lower()


def test_ac6_post_s2_outbound_is_sandboxed():
    r = privacy.privacy_report(profile=_profile(forged_net_jail="netns"), tier_models=_tiers())
    s = _section(r, "sandbox")
    assert s["status"] == "sandboxed" and "netns" in s["detail"]


# ── per-tier locality (the audit's F2 — the key trust fix) ───────────────────

def test_all_remote_says_prompts_leave_and_names_the_real_destination():
    r = privacy.privacy_report(profile=_profile(), tier_models=_tiers())
    s = _section(r, "llm")
    assert s["status"] == "leaves"
    assert r["destinations"] == ["openrouter.ai"]          # NOT "deepseek" (the vendor)
    assert "openrouter.ai" in s["detail"] and "openrouter.ai" in r["headline"]


def test_all_local_says_nothing_leaves():
    r = privacy.privacy_report(profile=_profile(), tier_models=_tiers(_LOCAL, _LOCAL, _LOCAL))
    s = _section(r, "llm")
    assert s["status"] == "local" and r["local_llm"] is True
    assert "do not leave" in s["detail"].lower() and r["destinations"] == []


def test_local_tier1_but_remote_others_still_leaks():
    # THE trust failure the audit caught: a local tier-1 must NOT claim "nothing leaves"
    # while tool-forge / formatting tiers still egress.
    r = privacy.privacy_report(profile=_profile(), tier_models=_tiers(_LOCAL, _REMOTE, _REMOTE))
    s = _section(r, "llm")
    assert s["status"] == "partial" and s["severity"] == "warn"
    assert r["local_llm"] is False                          # honest: data still leaves
    assert "openrouter.ai" in s["detail"] and "still leaves" in s["detail"].lower()


def test_anthropic_tier_names_anthropic():
    r = privacy.privacy_report(profile=_profile(),
                               tier_models=_tiers("anthropic/claude-sonnet-4.5", _REMOTE, _REMOTE))
    assert set(r["destinations"]) == {"anthropic", "openrouter.ai"}


# ── secrets-at-rest (the audit's F1 — plaintext fallback flagged) ────────────

def test_encrypted_secret_store_is_ok():
    for backend in ("dpapi", "keychain", "secretservice"):
        r = privacy.privacy_report(profile=_profile(keyring_backend=backend), tier_models=_tiers())
        assert _section(r, "secrets")["status"] == "encrypted"


def test_plaintext_fallback_is_flagged_using_the_real_runtime_value():
    # The runtime emits "plaintext_fallback" (platform_profile.KEYRING_PLAINTEXT) — NOT
    # a fabricated "file0600". This value MUST classify as plaintext/warn.
    r = privacy.privacy_report(profile=_profile(keyring_backend="plaintext_fallback"),
                               tier_models=_tiers())
    s = _section(r, "secrets")
    assert s["status"] == "plaintext" and s["severity"] == "warn"


# ── tool egress completeness (the audit's F4 — name the default relay) ───────

def test_tool_egress_names_the_default_third_parties():
    r = privacy.privacy_report(profile=_profile(), tier_models=_tiers())
    d = _section(r, "tools")["detail"].lower()
    assert "r.jina.ai" in d and "duckduckgo" in d and "openstreetmap" in d
    assert "mcp" in d and "approve" in d


# ── custody / container / no-key-leak ────────────────────────────────────────

def test_custody_section_always_present():
    r = privacy.privacy_report(profile=_profile(), tier_models=_tiers())
    assert _section(r, "custody")["status"] == "local"


def test_docker_mode_adds_host_companion_boundary():
    assert any(s["key"] == "docker" for s in
               privacy.privacy_report(profile=_profile(docker_mode=True), tier_models=_tiers())["sections"])
    assert not any(s["key"] == "docker" for s in
                   privacy.privacy_report(profile=_profile(docker_mode=False), tier_models=_tiers())["sections"])


def test_report_never_contains_a_key_value(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-supersecret-value-1234567890")
    import json
    r = privacy.privacy_report(profile=_profile(), tier_models=_tiers())
    assert "sk-supersecret-value-1234567890" not in json.dumps(r)
