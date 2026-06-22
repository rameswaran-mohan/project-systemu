"""Tests for v0.5.0-c — tool_inadequate classifier extension + LLM diagnosis."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from systemu.core.models import Shadow, ShadowStatus, Tool, ToolStatus, ToolType
from systemu.pipelines import tool_inadequacy_diagnosis as tid
from systemu.runtime.failure_classifier import (
    CATEGORIES, looks_like_inadequacy_signal,
)


@pytest.fixture(autouse=True)
def _reset():
    tid.reset_cache_for_tests()
    yield
    tid.reset_cache_for_tests()


@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _tool():
    return Tool(
        id="tool_x", name="x", description="t",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.DEPLOYED, enabled=True,
    )


def _shadow(sid="sh-1"):
    return Shadow(
        id=sid, name=sid, description="test shadow",
        status=ShadowStatus.AWAKENED,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Classifier extension

class TestClassifierCategory:
    def test_tool_inadequate_in_categories(self):
        assert "tool_inadequate" in CATEGORIES

    def test_pre_filter_skips_recoverable_categories(self):
        # missing_dependency is recoverable → never flagged as inadequacy
        assert looks_like_inadequacy_signal(
            "missing_dependency", "doesn't support custom formats"
        ) is False
        # param_error similarly excluded
        assert looks_like_inadequacy_signal(
            "param_error", "doesn't support that"
        ) is False
        # network_error excluded
        assert looks_like_inadequacy_signal(
            "network_error", "no way to retry"
        ) is False

    def test_pre_filter_triggers_on_inadequacy_hints(self):
        # unknown category + inadequacy hint → flagged
        assert looks_like_inadequacy_signal(
            "unknown", "Tool doesn't support PDF output"
        ) is True
        assert looks_like_inadequacy_signal(
            "unknown", "No way to handle dataframes"
        ) is True

    def test_pre_filter_no_hint_no_trigger(self):
        # Non-recoverable category but no hint words
        assert looks_like_inadequacy_signal(
            "unknown", "Generic error"
        ) is False
        # Empty text
        assert looks_like_inadequacy_signal("unknown", "") is False


# ─────────────────────────────────────────────────────────────────────────────
# diagnose_tool_inadequacy

class TestDiagnosisLLM:
    def test_returns_bump_version_for_universal_flaw(self, vault, monkeypatch):
        config = MagicMock()
        config.openrouter_api_key = "test"
        config.tier1_model = "test"

        def fake_llm(*, tier, system, user, config, temperature, max_tokens):
            return {
                "recalibration_mode": "bump_version",
                "rationale": "Missing error handling affects all callers",
                "spec_diff_summary": "Add try/except around the HTTP call",
                "affected_shadows": [],
                "confidence": "high",
            }
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json", fake_llm,
        )

        verdict = tid.diagnose_tool_inadequacy(
            tool=_tool(), shadow=_shadow(),
            config=config, vault=vault,
            execution_id="exec_test",
            failing_objective="generate a report",
            recent_failures=[],
        )
        assert verdict.is_inadequate is True
        assert verdict.recalibration_mode == "bump_version"
        assert verdict.confidence == "high"

    def test_returns_fork_for_specialised_need(self, vault, monkeypatch):
        config = MagicMock(); config.openrouter_api_key = "test"; config.tier1_model = "t"
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: {
                "recalibration_mode": "fork_new_tool",
                "rationale": "Shadow needs templating others don't use",
                "spec_diff_summary": "Add filename_template param",
                "new_tool_name_suggestion": "create_word_doc_templated",
                "affected_shadows": ["sh-other"],
                "confidence": "medium",
            },
        )
        verdict = tid.diagnose_tool_inadequacy(
            tool=_tool(), shadow=_shadow(),
            config=config, vault=vault, execution_id="exec_test",
            failing_objective="generate weather report with date templating",
            recent_failures=[],
        )
        assert verdict.is_inadequate is True
        assert verdict.recalibration_mode == "fork_new_tool"
        assert verdict.new_tool_name_suggestion == "create_word_doc_templated"

    def test_invalid_mode_returns_not_inadequate(self, vault, monkeypatch):
        config = MagicMock(); config.openrouter_api_key = "test"; config.tier1_model = "t"
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: {"recalibration_mode": "garbage", "rationale": "?"},
        )
        verdict = tid.diagnose_tool_inadequacy(
            tool=_tool(), shadow=_shadow(),
            config=config, vault=vault, execution_id="exec_test",
            failing_objective="...", recent_failures=[],
        )
        assert verdict.is_inadequate is False
        assert verdict.recalibration_mode == "none"

    def test_llm_failure_safe_default(self, vault, monkeypatch):
        config = MagicMock(); config.openrouter_api_key = "test"; config.tier1_model = "t"
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("network down")),
        )
        verdict = tid.diagnose_tool_inadequacy(
            tool=_tool(), shadow=_shadow(),
            config=config, vault=vault, execution_id="exec_test",
            failing_objective="...", recent_failures=[],
        )
        # Conservative: don't claim inadequacy on LLM error
        assert verdict.is_inadequate is False
        assert "error" in verdict.rationale.lower() or "fail" in verdict.rationale.lower()


class TestDiagnosisCache:
    def test_second_call_returns_cached(self, vault, monkeypatch):
        config = MagicMock(); config.openrouter_api_key = "test"; config.tier1_model = "t"
        call_count = {"i": 0}
        def fake_llm(**kw):
            call_count["i"] += 1
            return {
                "recalibration_mode": "fork_new_tool",
                "rationale": "x",
                "affected_shadows": [],
                "confidence": "high",
            }
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)

        for _ in range(3):
            tid.diagnose_tool_inadequacy(
                tool=_tool(), shadow=_shadow(),
                config=config, vault=vault, execution_id="exec_test",
                failing_objective="...", recent_failures=[],
            )
        # Cache fires after first call
        assert call_count["i"] == 1

    def test_different_executions_dont_share_cache(self, vault, monkeypatch):
        config = MagicMock(); config.openrouter_api_key = "test"; config.tier1_model = "t"
        call_count = {"i": 0}
        def fake_llm(**kw):
            call_count["i"] += 1
            return {
                "recalibration_mode": "bump_version",
                "rationale": "x", "affected_shadows": [], "confidence": "high",
            }
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)

        tid.diagnose_tool_inadequacy(
            tool=_tool(), shadow=_shadow(),
            config=config, vault=vault, execution_id="exec_A",
            failing_objective="...", recent_failures=[],
        )
        tid.diagnose_tool_inadequacy(
            tool=_tool(), shadow=_shadow(),
            config=config, vault=vault, execution_id="exec_B",
            failing_objective="...", recent_failures=[],
        )
        assert call_count["i"] == 2
