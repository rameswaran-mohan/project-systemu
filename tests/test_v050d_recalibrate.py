"""Tests for v0.5.0-d — recalibration pipeline + approval card publish.

Covers:
  * recalibrate_tool dispatches to _bump_version or _fork_new_tool
  * Bump regression triggers automatic fork fallback
  * Fork creates a new tool record with a distinct id
  * publish_recalibration_card emits the expected event shape
  * RECALIBRATE_TOOL is in ACTION_VOCABULARY and HIGH_IMPACT_ACTIONS
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from systemu.core.models import Shadow, ShadowStatus, Tool, ToolStatus, ToolType
from systemu.pipelines.tool_inadequacy_diagnosis import InadequacyDiagnosis
from systemu.pipelines import tool_recalibrator as tr
from systemu.runtime.execution_mind import (
    ACTION_VOCABULARY, HIGH_IMPACT_ACTIONS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary membership

class TestVocabulary:
    def test_recalibrate_tool_in_vocab(self):
        assert "RECALIBRATE_TOOL" in ACTION_VOCABULARY

    def test_recalibrate_tool_is_high_impact(self):
        assert "RECALIBRATE_TOOL" in HIGH_IMPACT_ACTIONS


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


@pytest.fixture
def config(tmp_path):
    c = MagicMock()
    c.vault_dir = str(tmp_path)
    c.openrouter_api_key = "test"
    c.tier1_model = "test"
    c.tier2_model = "test"
    c.tier3_model = "test"
    return c


def _tool(name="t", deps=None):
    t = Tool(
        id="tool_t", name=name, description="t",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.DEPLOYED, enabled=True,
        dependencies=list(deps or []),
        parameters_schema={"x": {"type": "string"}},
    )
    return t


def _shadow():
    return Shadow(id="sh-1", name="t", description="t", status=ShadowStatus.AWAKENED)


def _diagnosis(mode="bump_version", rationale="x", spec_summary="add err handling"):
    return InadequacyDiagnosis(
        is_inadequate=True,
        recalibration_mode=mode,
        rationale=rationale,
        spec_diff_summary=spec_summary,
        new_tool_name_suggestion="t_specialised",
        affected_shadows=[],
        confidence="high",
    )


# ─────────────────────────────────────────────────────────────────────────────
# bump_version path

class TestBumpVersionPath:
    def test_successful_bump(self, vault, config, monkeypatch):
        t = _tool()
        t.implementation_path = "vault/tools/implementations/t.py"
        vault.save_tool(t)

        # Stub forge re-spec + re-code by call order.
        call_order = {"i": 0}
        def fake_llm(*, tier, system, user, **kw):
            call_order["i"] += 1
            if call_order["i"] == 1:    # spec call
                return {"name": "t", "description": "new",
                         "parameters_schema": {"x": {"type": "string"}},
                         "return_schema": {"success": {"type": "boolean"}},
                         "implementation_notes": "new notes",
                         "dependencies": [], "tool_type": "python_function"}
            # code call
            return {"implementation": "def run(**kw):\n    return {'success': True}\n"}
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)
        # Stub dry-run + replay to pass.
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run.dry_run_tool",
            lambda tool, vault, config, prior_failure=None: SimpleNamespace(
                success=True, status="passed", error=None,
                to_evidence=lambda: {"success": True, "status": "passed"},
            ),
        )
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run.replay_against_history",
            lambda tool, vault, config: SimpleNamespace(
                success=True, status="passed", replayed_count=2, error=None,
            ),
        )

        result = tr.recalibrate_tool(
            tool=t, shadow=_shadow(), diagnosis=_diagnosis("bump_version"),
            failure_context="failed because X",
            config=config, vault=vault, execution_id="exec_test",
        )
        assert result.success is True
        assert result.mode == "bump_version"
        assert result.original_tool_id == t.id
        assert result.new_tool_id == t.id          # same id on bump
        assert result.replay_status == "passed"

    def test_bump_replay_regression_falls_back_to_fork(self, vault, config, monkeypatch):
        t = _tool()
        t.implementation_path = "vault/tools/implementations/t.py"
        t.last_successful_params = [{"x": "old"}]
        vault.save_tool(t)

        # Forge stubs OK — alternate spec/code by call order
        call_order = {"i": 0}
        def fake_llm(**kw):
            call_order["i"] += 1
            if call_order["i"] % 2 == 1:   # odd calls: spec
                return {"name": "t", "description": "d",
                         "parameters_schema": {"x": {"type": "string"}},
                         "return_schema": {"success": {"type": "boolean"}},
                         "implementation_notes": "n",
                         "dependencies": []}
            return {"implementation": "def run(**kw):\n    return {'success': True}\n"}
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)
        # Dry-run passes
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run.dry_run_tool",
            lambda tool, vault, config, prior_failure=None: SimpleNamespace(
                success=True, status="passed", error=None,
                to_evidence=lambda: {"success": True, "status": "passed"},
            ),
        )
        # Replay FAILS → triggers fork fallback
        replay_calls = {"n": 0}
        def fake_replay(tool, vault, config):
            replay_calls["n"] += 1
            return SimpleNamespace(
                success=False, status="failed", replayed_count=0,
                error="regression on entry 1",
            )
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run.replay_against_history", fake_replay,
        )

        result = tr.recalibrate_tool(
            tool=t, shadow=_shadow(), diagnosis=_diagnosis("bump_version"),
            failure_context="failed",
            config=config, vault=vault, execution_id="exec_test",
        )
        # Bump failed → fork attempted → fork dry-run also passes
        assert result.success is True
        assert result.mode == "fork_new_tool"
        assert result.forced_fallback is True
        assert result.replay_error == "regression on entry 1"
        # New tool was created with a distinct id
        assert result.new_tool_id != t.id


# ─────────────────────────────────────────────────────────────────────────────
# fork path direct (no fallback)

class TestForkPath:
    def test_fork_creates_new_tool(self, vault, config, monkeypatch):
        t = _tool()
        t.implementation_path = "vault/tools/implementations/t.py"
        vault.save_tool(t)

        call_order = {"i": 0}
        def fake_llm(**kw):
            call_order["i"] += 1
            if call_order["i"] % 2 == 1:
                return {"name": "fork_t", "description": "specialised",
                         "parameters_schema": {"x": {"type": "string"},
                                                "template": {"type": "string"}},
                         "return_schema": {"success": {"type": "boolean"}},
                         "implementation_notes": "n",
                         "dependencies": []}
            return {"implementation": "def run(**kw):\n    return {'success': True}\n"}
        monkeypatch.setattr("systemu.core.llm_router.llm_call_json", fake_llm)
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run.dry_run_tool",
            lambda tool, vault, config, prior_failure=None: SimpleNamespace(
                success=True, status="passed", error=None,
                to_evidence=lambda: {"success": True, "status": "passed"},
            ),
        )

        result = tr.recalibrate_tool(
            tool=t, shadow=_shadow(),
            diagnosis=_diagnosis("fork_new_tool"),
            failure_context="needs templating",
            config=config, vault=vault, execution_id="exec_test",
        )
        assert result.success is True
        assert result.mode == "fork_new_tool"
        assert result.new_tool_id != t.id    # new id
        # Original tool untouched
        original = vault.get_tool(t.id)
        assert original.version == 1


# ─────────────────────────────────────────────────────────────────────────────
# Approval card publish

class TestPublishCard:
    def test_publishes_approval_card_with_dedup_key(self, vault):
        from systemu.interface.event_bus import EventBus
        events: list = []
        unsub = EventBus.get().subscribe(lambda e: events.append(e), replay=False)
        try:
            result = tr.RecalibrationResult(
                success=True, mode="bump_version",
                original_tool_id="tool_x", new_tool_id="tool_x",
                new_tool_name="x", dry_run_status="passed",
                replay_status="passed",
                rationale="reason", spec_diff_summary="summary",
            )
            tr.publish_recalibration_card(
                result=result, shadow_id="sh-1",
                execution_id="exec_42", scroll_id="scr-9",
            )
        finally:
            unsub()
        approvals = [e for e in events if e.get("category") == "approval"]
        assert len(approvals) == 1
        ctx = approvals[0]["context"]
        assert ctx["dedup_key"] == "tool-recalibrate:tool_x:exec_42"
        assert ctx["redirect_to"] == "/tools"
        assert "enable_recalibrated_tool" in ctx["actions"]


# ─────────────────────────────────────────────────────────────────────────────
# Aborted path (diagnosis returns invalid mode)

class TestAbortedRecalibration:
    def test_diagnosis_says_not_inadequate_aborts(self, vault, config, monkeypatch):
        # Recalibrate even without inadequacy diagnosis (diagnosis "none")
        result = tr.recalibrate_tool(
            tool=_tool(), shadow=_shadow(),
            diagnosis=InadequacyDiagnosis(
                is_inadequate=False, recalibration_mode="none",
                rationale="false alarm", confidence="low",
            ),
            failure_context="",
            config=config, vault=vault, execution_id="exec_x",
        )
        assert result.success is False
        assert result.mode == "aborted"
