"""W14 S4 — validated-models trust anchor + per-provider boundary validation."""
from __future__ import annotations

import json

from systemu.runtime import model_validation as mv


def test_record_roundtrip(tmp_path):
    p = tmp_path / "validated_models.json"
    mv.record_validated(p, tier=1, provider="openrouter",
                        model="deepseek/deepseek-v4-flash",
                        validated_via="catalog", key_fingerprint="abc")
    assert mv.is_validated(p, provider="openrouter",
                           model="deepseek/deepseek-v4-flash") is True
    assert mv.is_validated(p, provider="openai", model="gpt-x") is False
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data[0]["validated_via"] == "catalog"
    assert "validated_at" in data[0]


def test_record_replaces_not_duplicates(tmp_path):
    p = tmp_path / "v.json"
    mv.record_validated(p, tier=1, provider="openrouter", model="m",
                        validated_via="catalog")
    mv.record_validated(p, tier=1, provider="openrouter", model="m",
                        validated_via="catalog")
    assert len(json.loads(p.read_text(encoding="utf-8"))) == 1


def test_validate_openrouter_uses_catalog(monkeypatch):
    monkeypatch.setattr(mv, "_fetch_openrouter_catalog",
                        lambda key, **kw: {"deepseek/deepseek-v4-flash"})
    ok, why = mv.validate_model(provider="openrouter",
                                model="deepseek/deepseek-v4-flash", credential="sk-or")
    assert ok and why == ""
    ok2, why2 = mv.validate_model(provider="openrouter", model="dead/model",
                                  credential="sk-or")
    assert not ok2 and "not available" in why2.lower()


def test_validate_blank_credential_is_config_error():
    ok, why = mv.validate_model(provider="openai", model="gpt-x", credential="")
    assert not ok and "key" in why.lower()


def test_prefix_mismatch_rejected_before_network():
    ok, why = mv.validate_model(provider="openai",
                                model="deepseek/deepseek-v4-flash", credential="sk-x")
    assert not ok and "mismatch" in why.lower()


def test_openrouter_namespaced_id_is_not_a_mismatch():
    # openrouter serves google/* etc — must NOT be flagged
    assert mv._prefix_mismatch("openrouter", "google/gemini-3-flash-preview") is False
    assert mv._prefix_mismatch("auto", "anthropic/claude-sonnet-4.5") is False


def test_fingerprint_is_not_the_key():
    fp = mv.key_fingerprint("sk-or-secret-value")
    assert "secret" not in fp and len(fp) == 12


def test_validated_via_per_provider():
    assert mv.validated_via_for("openrouter") == "catalog"
    assert mv.validated_via_for("") == "catalog"
    assert mv.validated_via_for("ollama") == "ollama_tags"
    assert mv.validated_via_for("anthropic") == "ping"
