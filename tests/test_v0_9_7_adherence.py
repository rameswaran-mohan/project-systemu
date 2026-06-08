"""Tests for Phase 3.2 — Execution-Adherence dial + Settings presets.

Covers:
  * resolve_adherence: explicit levels honored; auto + request_kind rules
  * Config.execution_adherence: default, env override, invalid → "auto"
  * ADHERENCE_PRESETS: 3 presets with coherent values
  * apply_preset: returns correct dict; raises KeyError on unknown name
"""

from __future__ import annotations

import os
import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_config():
    """Reload sharing_on.config so env changes are picked up in from_env()."""
    mods = [k for k in sys.modules if k.startswith("sharing_on")]
    for m in mods:
        del sys.modules[m]
    from sharing_on.config import Config
    return Config


def _fresh_adherence():
    """Re-import adherence module (needed after env manipulation)."""
    if "systemu.runtime.adherence" in sys.modules:
        del sys.modules["systemu.runtime.adherence"]
    from systemu.runtime import adherence
    return adherence


# ---------------------------------------------------------------------------
# resolve_adherence — explicit levels
# ---------------------------------------------------------------------------

class TestResolveAdherenceExplicit:
    """Explicit config values (free/guided/strict) win regardless of request_kind."""

    def setup_method(self):
        from systemu.runtime.adherence import resolve_adherence
        self.resolve = resolve_adherence

    def _cfg(self, level: str):
        return {"execution_adherence": level}

    def test_explicit_free(self):
        assert self.resolve(self._cfg("free"), request_kind="chat") == "free"

    def test_explicit_free_overrides_sop_kind(self):
        assert self.resolve(self._cfg("free"), request_kind="record", sop_adherence="strict") == "free"

    def test_explicit_guided(self):
        assert self.resolve(self._cfg("guided"), request_kind="chat") == "guided"

    def test_explicit_guided_overrides_sop_kind(self):
        assert self.resolve(self._cfg("guided"), request_kind="sop", sop_adherence="strict") == "guided"

    def test_explicit_strict(self):
        assert self.resolve(self._cfg("strict"), request_kind="chat") == "strict"

    def test_explicit_strict_overrides_chat(self):
        assert self.resolve(self._cfg("strict"), request_kind="chat") == "strict"


# ---------------------------------------------------------------------------
# resolve_adherence — auto mode
# ---------------------------------------------------------------------------

class TestResolveAdherenceAuto:
    """'auto' delegates to request_kind + sop_adherence."""

    def setup_method(self):
        from systemu.runtime.adherence import resolve_adherence
        self.resolve = resolve_adherence

    def _auto_cfg(self):
        return {"execution_adherence": "auto"}

    def test_auto_chat_returns_free(self):
        assert self.resolve(self._auto_cfg(), request_kind="chat") == "free"

    def test_auto_unknown_kind_returns_free(self):
        assert self.resolve(self._auto_cfg(), request_kind="unknown_future") == "free"

    def test_auto_record_no_sop_returns_guided(self):
        assert self.resolve(self._auto_cfg(), request_kind="record") == "guided"

    def test_auto_record_no_sop_explicit_none(self):
        assert self.resolve(self._auto_cfg(), request_kind="record", sop_adherence=None) == "guided"

    def test_auto_sop_kind_no_sop_adherence_returns_guided(self):
        assert self.resolve(self._auto_cfg(), request_kind="sop") == "guided"

    def test_auto_record_with_sop_strict(self):
        assert self.resolve(self._auto_cfg(), request_kind="record", sop_adherence="strict") == "strict"

    def test_auto_record_with_sop_free(self):
        assert self.resolve(self._auto_cfg(), request_kind="record", sop_adherence="free") == "free"

    def test_auto_record_with_sop_guided(self):
        assert self.resolve(self._auto_cfg(), request_kind="record", sop_adherence="guided") == "guided"

    def test_auto_sop_kind_invalid_sop_adherence_falls_back_to_guided(self):
        assert self.resolve(self._auto_cfg(), request_kind="sop", sop_adherence="banana") == "guided"

    def test_auto_default_config_attribute(self):
        """Works with object that has execution_adherence attribute (like Config)."""
        class FakeCfg:
            execution_adherence = "auto"
        assert self.resolve(FakeCfg(), request_kind="chat") == "free"

    def test_missing_attribute_treated_as_auto(self):
        """Object without execution_adherence → treated as 'auto'."""
        class NoCfg:
            pass
        assert self.resolve(NoCfg(), request_kind="chat") == "free"

    def test_none_config_treated_as_auto(self):
        """None config → treated as 'auto'."""
        assert self.resolve(None, request_kind="chat") == "free"

    def test_none_config_record_returns_guided(self):
        assert self.resolve(None, request_kind="record") == "guided"


# ---------------------------------------------------------------------------
# Config.execution_adherence field
# ---------------------------------------------------------------------------

class TestConfigExecutionAdherence:
    """Config dataclass field: default, env override, invalid → auto."""

    def test_default_is_auto(self):
        """Default value when env var is absent."""
        env_key = "SYSTEMU_EXECUTION_ADHERENCE"
        old = os.environ.pop(env_key, None)
        try:
            Config = _reload_config()
            cfg = Config()
            assert cfg.execution_adherence == "auto"
        finally:
            if old is not None:
                os.environ[env_key] = old

    def test_from_env_default_is_auto(self):
        env_key = "SYSTEMU_EXECUTION_ADHERENCE"
        old = os.environ.pop(env_key, None)
        try:
            Config = _reload_config()
            cfg = Config.from_env()
            assert cfg.execution_adherence == "auto"
        finally:
            if old is not None:
                os.environ[env_key] = old

    @pytest.mark.parametrize("level", ["free", "guided", "strict"])
    def test_env_override(self, level: str):
        env_key = "SYSTEMU_EXECUTION_ADHERENCE"
        old = os.environ.pop(env_key, None)
        try:
            os.environ[env_key] = level
            Config = _reload_config()
            cfg = Config()
            assert cfg.execution_adherence == level
        finally:
            if old is not None:
                os.environ[env_key] = old
            else:
                os.environ.pop(env_key, None)

    @pytest.mark.parametrize("bad_value", ["banana", "replay", "0", "", "  "])
    def test_invalid_env_falls_back_to_auto(self, bad_value: str):
        env_key = "SYSTEMU_EXECUTION_ADHERENCE"
        old = os.environ.pop(env_key, None)
        try:
            os.environ[env_key] = bad_value
            Config = _reload_config()
            cfg = Config()
            assert cfg.execution_adherence == "auto"
        finally:
            if old is not None:
                os.environ[env_key] = old
            else:
                os.environ.pop(env_key, None)


# ---------------------------------------------------------------------------
# ADHERENCE_PRESETS
# ---------------------------------------------------------------------------

class TestAdherencePresets:
    """Verify preset structure and coherence."""

    def setup_method(self):
        from systemu.runtime.adherence import ADHERENCE_PRESETS, apply_preset
        self.presets = ADHERENCE_PRESETS
        self.apply = apply_preset

    def test_three_presets_exist(self):
        assert set(self.presets.keys()) == {"locked_sop", "assisted", "autonomous"}

    # ── locked_sop ───────────────────────────────────────────────────────────

    def test_locked_sop_adherence_is_strict(self):
        assert self.presets["locked_sop"]["SYSTEMU_EXECUTION_ADHERENCE"] == "strict"

    def test_locked_sop_no_auto_grants(self):
        p = self.presets["locked_sop"]
        for key in (
            "SYSTEMU_HARNESS_AUTO_GRANT_TOOL",
            "SYSTEMU_HARNESS_AUTO_GRANT_SKILL",
            "SYSTEMU_HARNESS_AUTO_GRANT_ACCESS",
            "SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE",
            "SYSTEMU_HARNESS_AUTO_GRANT_SUBAGENT",
        ):
            assert key in p, f"Missing key {key} in locked_sop preset"
            assert p[key].lower() in ("false", "0", "no", "off"), (
                f"locked_sop preset should disable {key}, got {p[key]!r}"
            )

    # ── assisted ─────────────────────────────────────────────────────────────

    def test_assisted_adherence_is_guided(self):
        assert self.presets["assisted"]["SYSTEMU_EXECUTION_ADHERENCE"] == "guided"

    def test_assisted_has_auto_grant_skill_and_compute(self):
        p = self.presets["assisted"]
        assert p["SYSTEMU_HARNESS_AUTO_GRANT_SKILL"].lower() in ("true", "1", "yes", "on")
        assert p["SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE"].lower() in ("true", "1", "yes", "on")

    def test_assisted_no_auto_grant_tool(self):
        p = self.presets["assisted"]
        assert p["SYSTEMU_HARNESS_AUTO_GRANT_TOOL"].lower() in ("false", "0", "no", "off")

    # ── autonomous ───────────────────────────────────────────────────────────

    def test_autonomous_adherence_is_free(self):
        assert self.presets["autonomous"]["SYSTEMU_EXECUTION_ADHERENCE"] == "free"

    def test_autonomous_auto_grant_skill_compute_access_subagent(self):
        p = self.presets["autonomous"]
        for key in (
            "SYSTEMU_HARNESS_AUTO_GRANT_SKILL",
            "SYSTEMU_HARNESS_AUTO_GRANT_ACCESS",
            "SYSTEMU_HARNESS_AUTO_GRANT_COMPUTE",
            "SYSTEMU_HARNESS_AUTO_GRANT_SUBAGENT",
        ):
            assert key in p, f"Missing key {key} in autonomous preset"
            assert p[key].lower() in ("true", "1", "yes", "on"), (
                f"autonomous preset should enable {key}, got {p[key]!r}"
            )

    # ── apply_preset ─────────────────────────────────────────────────────────

    def test_apply_preset_returns_copy(self):
        result = self.apply("locked_sop")
        assert isinstance(result, dict)
        # Mutating the result should not affect the original
        result["EXTRA"] = "mutated"
        assert "EXTRA" not in self.presets["locked_sop"]

    def test_apply_preset_unknown_raises_key_error(self):
        with pytest.raises(KeyError, match="Unknown preset"):
            self.apply("does_not_exist")

    @pytest.mark.parametrize("name", ["locked_sop", "assisted", "autonomous"])
    def test_apply_preset_all_names(self, name: str):
        result = self.apply(name)
        assert "SYSTEMU_EXECUTION_ADHERENCE" in result
