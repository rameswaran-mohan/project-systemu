"""W11.7 — field RCA fixes: why every chat test failed on a live install.

Field evidence (operator tryout, 2026-06-12): both quick-lane chat runs
failed. The causal chain, verified against their vault + daemon log:

  1. quick_task built its ToolSandbox WITHOUT install_mode/approvals —
     PROMPT mode with approvals=None fail-closes EVERY dep-declaring tool
     (fetch_json/web_read/web_extract all "blocked_pending_approval") even
     though the operator HAD approved requests+playwright on June 2 and the
     packages WERE installed. The default chat lane forked the sandbox
     *construction* (the W6 lesson covered the execution path only).
  2. _is_satisfied was an in-process cache, not an installed-check — every
     fresh daemon re-gated already-installed packages forever.
  3. With web tools blocked, the model fell back to `run_command` curl
     hacks → timeout → two failures reporting `error: None` (unactionable).
  4. The no-env default tier-3 model (z-ai/glm-4.5-air:free) returns 404
     "This model is unavailable" from OpenRouter — web_extract and every
     other tier-3 consumer was dead on default installs.
"""
from __future__ import annotations

import inspect

import pytest


class TestQuickLaneSandboxWiring:
    def test_quick_lane_resolves_installer_policy(self):
        from systemu.pipelines import quick_task
        src = inspect.getsource(quick_task.run_quick_task)
        assert "resolve_install_mode" in src, \
            "the quick lane must use the SAME installer policy as the runtime"
        assert "init_default_store" in src, \
            "without the approval store, PROMPT mode fail-closes every dep tool"
        assert "install_mode=" in src and "approvals=" in src


class TestInstalledPackagesAreSatisfied:
    def setup_method(self):
        from systemu.runtime.dependency_installer import reset_cache_for_tests
        reset_cache_for_tests()

    def test_installed_package_is_satisfied(self):
        from systemu.runtime.dependency_installer import _is_satisfied
        assert _is_satisfied("pytest") is True, \
            "a package already in site-packages must never need approval"

    def test_version_spec_strips_to_distribution_name(self):
        from systemu.runtime.dependency_installer import _is_satisfied
        assert _is_satisfied("pytest>=1.0") is True

    def test_missing_package_is_not_satisfied(self):
        from systemu.runtime.dependency_installer import _is_satisfied
        assert _is_satisfied("definitely-not-a-real-package-xyz123") is False

    def test_prompt_mode_without_store_passes_installed_packages(self):
        """THE field case: requests installed + approvals=None must succeed,
        not block."""
        from systemu.runtime.dependency_installer import (
            InstallMode, InstallStatus, ensure_satisfied)
        result = ensure_satisfied(
            ["pytest"], mode=InstallMode.PROMPT, approvals=None,
            tool_name="field_case")
        assert result.ok is True
        assert result.status is InstallStatus.SATISFIED


class TestFailureErrorsAreActionable:
    def test_error_passthrough(self):
        from systemu.pipelines.quick_task import _failure_error
        assert _failure_error("boom", {}) == "boom"

    def test_stderr_tail_fallback(self):
        from systemu.pipelines.quick_task import _failure_error
        assert "curl: timeout" in _failure_error(
            None, {"stderr": "long...\ncurl: timeout"})

    def test_exit_code_fallback(self):
        from systemu.pipelines.quick_task import _failure_error
        assert "exit 7" in _failure_error(None, {"returncode": 7})

    def test_never_the_string_none(self):
        from systemu.pipelines.quick_task import _failure_error
        msg = _failure_error(None, {})
        assert msg and "None" not in msg, \
            "field report: 'run_command failed 3 times in a row: None'"


class TestNoDeadDefaultModels:
    DEAD = "z-ai/glm-4.5-air:free"   # 404 'This model is unavailable' (field telemetry)

    def test_no_env_defaults_carry_no_dead_model(self):
        from sharing_on.model_presets import resolve_preset
        assert self.DEAD not in resolve_preset({}).values()

    def test_no_preset_carries_the_dead_model(self):
        from sharing_on.model_presets import PRESETS
        for name, tiers in PRESETS.items():
            assert self.DEAD not in tiers.values(), f"preset {name!r}"

    def test_config_defaults_carry_no_dead_model(self):
        import sharing_on.config as cfg
        src = inspect.getsource(cfg)
        assert f'= "{self.DEAD}"' not in src and f'"{self.DEAD}")' not in src, \
            "Config dataclass/env defaults still point at the 404 model"
