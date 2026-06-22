"""Tests for v0.4.0-c — scroll validator + diagnosis-to-memory.

Covers:
  1. Scroll validator's opt-in resolution
  2. Validator's parse path on synthetic LLM output
  3. Validator's empty-objectives early exit (no LLM call)
  4. Validator returns satisfiable=True when disabled (fail-open default)
  5. Validator fail-open on LLM error (doesn't block the pipeline)
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.pipelines import scroll_validator as sv


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills",
                "tools", "evolutions", "elder"):
        (tmp_path / sub).mkdir()
        if sub != "elder":
            (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _config(*, supervisor_on=False, scroll_validator=False):
    c = MagicMock()
    c.intelligent_supervisor_enabled = supervisor_on
    # v0.6.5-d: tests in this file pre-date the dedicated scroll_validator
    # config field — default it False here to preserve the legacy semantics
    # they're asserting (off-by-default-without-supervisor).
    c.scroll_validator = scroll_validator
    c.openrouter_api_key = "test"
    c.tier1_model = "gpt-test"
    return c


def _scroll(objectives=None, name="test"):
    objs = []
    for spec in (objectives or []):
        # Use a SimpleNamespace so getattr-based access in validate_scroll works
        objs.append(SimpleNamespace(**spec))
    return SimpleNamespace(
        name=name,
        intent="test intent",
        objectives=objs,
        constraints={},
        id="scroll_test",
    )


# ─────────────────────────────────────────────────────────────────────────────
# is_enabled

class TestIsEnabled:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_SCROLL_VALIDATOR", raising=False)
        assert sv.is_enabled(_config(supervisor_on=False)) is False

    def test_supervisor_enabled_turns_on(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_SCROLL_VALIDATOR", raising=False)
        assert sv.is_enabled(_config(supervisor_on=True)) is True

    def test_env_var_overrides_config(self, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        assert sv.is_enabled(_config(supervisor_on=False)) is True
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "0")
        assert sv.is_enabled(_config(supervisor_on=True)) is False


# ─────────────────────────────────────────────────────────────────────────────
# validate_scroll

class TestValidateScroll:
    def test_disabled_returns_satisfiable_true(self, vault, monkeypatch):
        monkeypatch.delenv("SYSTEMU_SCROLL_VALIDATOR", raising=False)
        scroll = _scroll([{"id": 1, "goal": "x", "success_criteria": "y"}])
        result = sv.validate_scroll(scroll, config=_config(supervisor_on=False), vault=vault)
        assert result.satisfiable is True
        assert result.confidence == "low"
        assert "disabled" in result.summary

    def test_empty_objectives_short_circuits(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll([])
        result = sv.validate_scroll(scroll, config=_config(), vault=vault)
        assert result.satisfiable is False
        assert len(result.blockers) == 1
        assert result.blockers[0].category == "other"

    def test_parse_satisfiable_true(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll([{"id": 1, "goal": "g", "success_criteria": "sc"}])

        def fake_llm(*, tier, system, user, config, temperature, max_tokens):
            return {
                "satisfiable": True,
                "confidence": "high",
                "blockers":   [],
                "summary":    "looks good",
            }
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", fake_llm,
        )
        result = sv.validate_scroll(scroll, config=_config(), vault=vault)
        assert result.satisfiable is True
        assert result.confidence == "high"
        assert result.summary == "looks good"

    def test_parse_with_blockers(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll([{"id": 1, "goal": "g", "success_criteria": "sc"}])

        def fake_llm(*, tier, system, user, config, temperature, max_tokens):
            return {
                "satisfiable": False,
                "confidence":  "high",
                "blockers": [
                    {
                        "objective_id":  1,
                        "category":      "no_tool",
                        "explanation":   "no tool for X",
                        "suggested_fix": "forge tool X",
                    },
                ],
                "summary": "missing tool",
            }
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", fake_llm,
        )
        result = sv.validate_scroll(scroll, config=_config(), vault=vault)
        assert result.satisfiable is False
        assert len(result.blockers) == 1
        assert result.blockers[0].category == "no_tool"

    def test_llm_failure_fails_open(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll([{"id": 1, "goal": "g", "success_criteria": "sc"}])

        def boom(*, tier, system, user, config, temperature, max_tokens):
            raise RuntimeError("network down")
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", boom,
        )
        result = sv.validate_scroll(scroll, config=_config(), vault=vault)
        # fail-open: don't block the pipeline on validator errors
        assert result.satisfiable is True
        assert "fail-open" in result.summary
        assert result.error is not None

    def test_non_dict_output_fails_open(self, vault, monkeypatch):
        monkeypatch.setenv("SYSTEMU_SCROLL_VALIDATOR", "1")
        scroll = _scroll([{"id": 1, "goal": "g", "success_criteria": "sc"}])
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: "this is not a dict",
        )
        result = sv.validate_scroll(scroll, config=_config(), vault=vault)
        assert result.satisfiable is True
        assert result.error is not None


# ─────────────────────────────────────────────────────────────────────────────
# Diagnosis-to-memory write (unit on the parse + signature path, not the
# full supervisor LLM call which costs real money in CI)

class TestDiagnosisToMemorySignature:
    def test_signature_from_failure_category(self):
        from systemu.core.memory_types import pattern_signature
        sig = pattern_signature(
            error_type="scroll_flaw",
            tool_name=None,
            error_message="objective 2 has unmeasurable success_criteria",
        )
        # error_type|tool|first_keyword
        assert sig.startswith("scroll_flaw|unknown|")
