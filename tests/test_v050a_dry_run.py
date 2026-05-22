"""Tests for v0.5.0-a — tool dry-run pipeline.

Covers:
  * Tool model gains dry_run_status / dry_run_evidence / last_successful_params / evolution_history
  * Vault round-trip preserves them
  * Schema-driven fallback when LLM unavailable
  * Destructive tool without dry_run param → skipped
  * Path-like args get sandboxed to /tmp
  * Successful execute → status=passed; failed execute → status=failed
  * Replay against history returns success only when all entries still pass
  * Tool registry refuses to call a tool whose dry_run_status=failed
  * record_successful_params appends and caps + redacts secrets
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from systemu.core.models import Tool, ToolStatus, ToolType


# ─────────────────────────────────────────────────────────────────────────────
# Model + round-trip

@pytest.fixture
def vault(tmp_path):
    from systemu.vault.vault import Vault
    for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                "evolutions"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "index.json").write_text("[]")
    return Vault(str(tmp_path))


def _make_tool(name="t", deps=None):
    return Tool(
        id=f"tool_{name}",
        name=name,
        description="for tests",
        tool_type=ToolType.PYTHON_FUNCTION,
        status=ToolStatus.FORGED,
        enabled=False,
        dependencies=list(deps or []),
        parameters_schema={"x": {"type": "string", "default": "hello"}},
    )


class TestModelDefaults:
    def test_new_fields_default(self):
        t = _make_tool()
        assert t.dry_run_status == "not_run"
        assert t.dry_run_evidence == {}
        assert t.last_successful_params == []
        assert t.evolution_history == []

    def test_legacy_data_loads(self):
        legacy = {
            "id": "tool_x", "name": "t", "description": "d",
            "tool_type": "python_function", "status": "proposed",
        }
        t = Tool.model_validate(legacy)
        assert t.dry_run_status == "not_run"
        assert t.last_successful_params == []


class TestVaultRoundTrip:
    def test_round_trip_through_file_vault(self, vault):
        t = _make_tool()
        t.dry_run_status = "passed"
        t.dry_run_evidence = {"elapsed_ms": 12, "success": True}
        t.last_successful_params = [{"x": "first"}, {"x": "second"}]
        t.evolution_history = [{"version": 1, "reason": "initial"}]
        vault.save_tool(t)
        loaded = vault.get_tool("tool_t")
        assert loaded.dry_run_status == "passed"
        assert loaded.dry_run_evidence["elapsed_ms"] == 12
        assert len(loaded.last_successful_params) == 2
        assert loaded.evolution_history[0]["reason"] == "initial"


# ─────────────────────────────────────────────────────────────────────────────
# Schema-driven fallback when LLM fails

class TestSchemaFallback:
    def test_picks_defaults_when_llm_errors(self, vault):
        from systemu.pipelines.tool_dry_run import _generate_test_params

        t = _make_tool()
        t.parameters_schema = {
            "name":    {"type": "string"},
            "count":   {"type": "integer"},
            "active":  {"type": "boolean"},
            "items":   {"type": "array"},
            "config":  {"type": "object"},
        }
        config = MagicMock()
        config.openrouter_api_key = "test"
        config.tier3_model = "test"

        with patch("systemu.core.llm_router.llm_call_json",
                   side_effect=RuntimeError("boom")):
            params, meta = _generate_test_params(t, config=config)

        assert params["name"] == ""
        assert params["count"] == 0
        assert params["active"] is False
        assert params["items"] == []
        assert params["config"] == {}
        assert meta == {}    # no skip flag

    def test_schema_default_used_when_present(self):
        from systemu.pipelines.tool_dry_run import _schema_default_params

        schema = {"name": {"type": "string", "default": "Hello"}}
        params = _schema_default_params(schema)
        assert params["name"] == "Hello"


# ─────────────────────────────────────────────────────────────────────────────
# Path sandboxing

class TestPathSandbox:
    def test_path_like_args_redirected_to_tmp(self):
        from systemu.pipelines.tool_dry_run import _sandbox_paths

        out = _sandbox_paths({
            "output_path": "real_output.docx",
            "file_path":   "important.txt",
            "x":           "not_a_path",
        })
        # Rewritten to tmp paths with same extensions (Windows uses Temp/, Linux /tmp)
        import tempfile
        tmp_root = tempfile.gettempdir().lower()
        assert out["output_path"].endswith(".docx")
        assert out["output_path"].lower().startswith(tmp_root) or "tmp" in out["output_path"].lower()
        assert out["file_path"].endswith(".txt")
        assert out["x"] == "not_a_path"   # unaffected


# ─────────────────────────────────────────────────────────────────────────────
# dry_run_tool top-level behaviour

class TestDryRunTopLevel:
    def test_missing_implementation_path_skipped(self, vault):
        from systemu.pipelines.tool_dry_run import dry_run_tool

        t = _make_tool()
        t.implementation_path = ""
        config = MagicMock()
        config.openrouter_api_key = "test"
        config.tier3_model = "test"
        result = dry_run_tool(t, vault=vault, config=config)
        assert result.status == "skipped"
        assert "no implementation_path" in (result.skip_reason or "")

    def test_destructive_without_dry_run_skipped(self, vault, monkeypatch):
        from systemu.pipelines.tool_dry_run import dry_run_tool
        t = _make_tool(name="delete_user_account")     # name triggers destructive heuristic
        t.implementation_path = "vault/tools/implementations/delete_user_account.py"
        config = MagicMock()
        config.openrouter_api_key = "test"
        config.tier3_model = "test"
        config.vault_dir = "vault"

        # LLM returns params without dry_run flag
        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: {"params": {"user_id": "test"}, "rationale": "ok"},
        )
        result = dry_run_tool(t, vault=vault, config=config)
        assert result.status == "skipped"
        assert "destructive" in (result.skip_reason or "").lower()

    def test_llm_recommends_skip(self, vault, monkeypatch):
        from systemu.pipelines.tool_dry_run import dry_run_tool
        t = _make_tool()
        t.implementation_path = "vault/tools/implementations/t.py"
        config = MagicMock()
        config.openrouter_api_key = "test"
        config.tier3_model = "test"
        config.vault_dir = "vault"

        monkeypatch.setattr(
            "systemu.core.llm_router.llm_call_json",
            lambda **kw: {"skip_dry_run": True, "skip_reason": "test reason"},
        )
        result = dry_run_tool(t, vault=vault, config=config)
        assert result.status == "skipped"
        assert "test reason" in (result.skip_reason or "")


# ─────────────────────────────────────────────────────────────────────────────
# Replay against history (used by v0.5.0-d bump-version path)

class TestReplayAgainstHistory:
    def test_no_history_returns_pass(self, vault):
        from systemu.pipelines.tool_dry_run import replay_against_history
        t = _make_tool()
        t.last_successful_params = []
        config = MagicMock(); config.openrouter_api_key = "test"
        result = replay_against_history(t, vault=vault, config=config)
        assert result.status == "passed"
        assert result.replayed_count == 0

    def test_all_history_passes(self, vault, monkeypatch):
        from systemu.pipelines.tool_dry_run import replay_against_history
        t = _make_tool()
        t.implementation_path = "vault/tools/implementations/t.py"
        t.last_successful_params = [{"x": "1"}, {"x": "2"}, {"x": "3"}]
        config = MagicMock(); config.openrouter_api_key = "test"; config.vault_dir = "vault"

        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run._execute",
            lambda tool, params, vault, config: {"success": True},
        )
        result = replay_against_history(t, vault=vault, config=config)
        assert result.status == "passed"
        assert result.replayed_count == 3

    def test_one_regression_fails_whole_replay(self, vault, monkeypatch):
        from systemu.pipelines.tool_dry_run import replay_against_history
        t = _make_tool()
        t.implementation_path = "vault/tools/implementations/t.py"
        t.last_successful_params = [{"x": "1"}, {"x": "bad"}, {"x": "3"}]
        config = MagicMock(); config.openrouter_api_key = "test"; config.vault_dir = "vault"

        calls = {"i": 0}
        def fake_exec(*, tool=None, params=None, vault=None, config=None, **kw):
            calls["i"] += 1
            return {"success": calls["i"] != 2, "error": "regression"}
        # _execute signature is positional in dry_run, adapt
        monkeypatch.setattr(
            "systemu.pipelines.tool_dry_run._execute",
            lambda tool, params, vault, config: {
                "success": params.get("x") != "bad",
                "error":   "regression" if params.get("x") == "bad" else None,
            },
        )
        result = replay_against_history(t, vault=vault, config=config)
        assert result.status == "failed"
        assert "regression" in (result.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# Tool registry refuses failed-dry-run tools

class TestRegistryRefuses:
    def test_dry_run_failed_tool_blocked(self, vault, tmp_path):
        from systemu.runtime.tool_registry import ToolRegistry
        from systemu.runtime import dependency_installer as di

        t = _make_tool()
        t.status = ToolStatus.DEPLOYED
        t.enabled = True
        t.dry_run_status = "failed"
        t.dry_run_evidence = {"error": "tool blew up"}
        vault.save_tool(t)

        impl_dir = tmp_path / "tools" / "implementations"
        impl_dir.mkdir(parents=True, exist_ok=True)
        registry = ToolRegistry(impl_dir, vault, install_mode=di.InstallMode.ALWAYS)

        import asyncio
        result = asyncio.run(registry.execute("t", {}))
        assert result["success"] is False
        assert result["error_type"] == "tool_dry_run_failed"


# ─────────────────────────────────────────────────────────────────────────────
# record_successful_params

class TestRecordSuccessfulParams:
    def test_appends_and_caps(self, vault):
        from systemu.pipelines.tool_dry_run import record_successful_params, _MAX_HISTORY_PER_TOOL

        t = _make_tool()
        vault.save_tool(t)
        for i in range(_MAX_HISTORY_PER_TOOL + 5):
            record_successful_params(t, {"i": i}, vault)
        reloaded = vault.get_tool("tool_t")
        assert len(reloaded.last_successful_params) == _MAX_HISTORY_PER_TOOL
        # Newest entries retained
        assert reloaded.last_successful_params[-1]["i"] == _MAX_HISTORY_PER_TOOL + 4

    def test_secrets_redacted(self, vault):
        from systemu.pipelines.tool_dry_run import record_successful_params

        t = _make_tool()
        vault.save_tool(t)
        record_successful_params(t, {
            "api_key": "sk-real-key-12345",
            "username": "alice",
            "secret_token": "abcd",
            "password": "hunter2",
            "data": "hello",
        }, vault)
        reloaded = vault.get_tool("tool_t")
        entry = reloaded.last_successful_params[0]
        assert entry["api_key"] == "<redacted>"
        assert entry["secret_token"] == "<redacted>"
        assert entry["password"] == "<redacted>"
        assert entry["username"] == "alice"      # not secret-like
        assert entry["data"] == "hello"
