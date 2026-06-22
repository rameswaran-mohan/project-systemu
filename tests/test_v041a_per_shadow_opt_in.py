"""Tests for v0.4.1-a per-shadow supervisor opt-in.

Validates:
  * Shadow.supervisor_enabled defaults False and round-trips through both vaults
  * ExecutionMind respects force_enabled override when global config is off
  * Existing Shadows without the field load as supervisor_enabled=False (forward-compat)
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from systemu.core.models import Shadow, ShadowStatus
from systemu.runtime.execution_mind import ExecutionMind


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic model

class TestShadowModel:
    def test_default_false(self):
        sh = Shadow(id="sh-1", name="X", description="t")
        assert sh.supervisor_enabled is False

    def test_explicit_true(self):
        sh = Shadow(id="sh-1", name="X", description="t", supervisor_enabled=True)
        assert sh.supervisor_enabled is True

    def test_pre_v041_data_compatible(self):
        """Loading a JSON dict that pre-dates v0.4.1 must succeed with
        supervisor_enabled defaulting to False."""
        legacy = {"id": "sh-x", "name": "n", "description": "d"}
        sh = Shadow.model_validate(legacy)
        assert sh.supervisor_enabled is False

    def test_round_trips_through_model_dump(self):
        sh = Shadow(id="sh-1", name="X", description="t", supervisor_enabled=True)
        data = sh.model_dump()
        assert data["supervisor_enabled"] is True
        sh2 = Shadow.model_validate(data)
        assert sh2.supervisor_enabled is True


# ─────────────────────────────────────────────────────────────────────────────
# File vault round-trip

class TestFileVaultRoundTrip:
    def test_round_trips_through_file_vault(self, tmp_path):
        from systemu.vault.vault import Vault
        for sub in ("scrolls", "activities", "shadow_army", "skills", "tools",
                    "evolutions"):
            (tmp_path / sub).mkdir()
            (tmp_path / sub / "index.json").write_text("[]")
        v = Vault(str(tmp_path))
        sh = Shadow(
            id="sh-1", name="X", description="t",
            supervisor_enabled=True,
            status=ShadowStatus.AWAKENED,
        )
        v.save_shadow(sh)
        loaded = v.get_shadow("sh-1")
        assert loaded.supervisor_enabled is True


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionMind force_enabled override

class TestExecutionMindForceEnabled:
    def test_global_off_and_force_off_disables_mind(self, tmp_path):
        config = SimpleNamespace(
            intelligent_supervisor_enabled=False,
            supervisor_llm_budget_per_run=10,
            supervisor_tier_routine="tier_3",
            supervisor_tier_intervention="tier_1",
            supervisor_directive_timeout_s=1.0,
        )
        mind = ExecutionMind(
            execution_id="exec_test",
            shadow_id="sh-1",
            config=config,
            directive_sink=lambda d: None,
            data_dir=tmp_path,
            force_enabled=False,
        )
        assert mind.enabled is False

    def test_global_off_but_force_on_enables_mind(self, tmp_path):
        """The v0.4.1-a contract: per-shadow opt-in works even when the
        global config flag is off."""
        config = SimpleNamespace(
            intelligent_supervisor_enabled=False,
            supervisor_llm_budget_per_run=10,
            supervisor_tier_routine="tier_3",
            supervisor_tier_intervention="tier_1",
            supervisor_directive_timeout_s=1.0,
        )
        mind = ExecutionMind(
            execution_id="exec_test",
            shadow_id="sh-1",
            config=config,
            directive_sink=lambda d: None,
            data_dir=tmp_path,
            force_enabled=True,
        )
        assert mind.enabled is True

    def test_global_on_remains_on_regardless_of_force(self, tmp_path):
        """When the global flag is on, force_enabled is redundant but harmless."""
        config = SimpleNamespace(
            intelligent_supervisor_enabled=True,
            supervisor_llm_budget_per_run=10,
            supervisor_tier_routine="tier_3",
            supervisor_tier_intervention="tier_1",
            supervisor_directive_timeout_s=1.0,
        )
        for force in (True, False):
            mind = ExecutionMind(
                execution_id="exec_test",
                shadow_id="sh-1",
                config=config,
                directive_sink=lambda d: None,
                data_dir=tmp_path,
                force_enabled=force,
            )
            assert mind.enabled is True
