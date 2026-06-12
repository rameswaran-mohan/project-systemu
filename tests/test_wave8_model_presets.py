"""W8.1 — model presets + flash-brain advisory.

The agent's "deep reasoning" tier defaults to a flash-class model and the
choice is invisible. Presets make the quality/cost tradeoff one keystroke
(SYSTEMU_MODEL_PRESET=quality|balanced|budget) WITHOUT silently changing any
paid default: no preset env ⇒ today's models byte-for-byte, and explicit
SYSTEMU_TIER*_MODEL overrides always win over a preset.
"""
from __future__ import annotations

import inspect

import pytest

from sharing_on.model_presets import PRESETS, is_budget_class, resolve_preset

# Today's shipped defaults — the back-compat contract.
_TODAY = {
    "tier1": "deepseek/deepseek-v4-flash",
    "tier2": "deepseek/deepseek-v4-flash",
    "tier3": "z-ai/glm-4.5-air:free",
}


class TestResolvePreset:
    def test_no_preset_env_is_todays_defaults_exactly(self):
        assert resolve_preset({}) == _TODAY

    def test_budget_preset_equals_no_preset(self):
        assert resolve_preset({"SYSTEMU_MODEL_PRESET": "budget"}) == _TODAY

    def test_quality_preset_upgrades_all_tiers(self):
        tiers = resolve_preset({"SYSTEMU_MODEL_PRESET": "quality"})
        assert tiers == PRESETS["quality"]
        assert not is_budget_class(tiers["tier1"]), \
            "quality preset must put a non-flash-class model in the reasoning tier"

    def test_unknown_preset_falls_back_to_defaults(self):
        assert resolve_preset({"SYSTEMU_MODEL_PRESET": "turbo-max"}) == _TODAY

    def test_case_and_whitespace_tolerant(self):
        assert resolve_preset({"SYSTEMU_MODEL_PRESET": " Quality "}) == PRESETS["quality"]

    def test_all_presets_define_all_three_tiers(self):
        for name, tiers in PRESETS.items():
            assert set(tiers) == {"tier1", "tier2", "tier3"}, name


class TestIsBudgetClass:
    @pytest.mark.parametrize("model,expected", [
        ("deepseek/deepseek-v4-flash", True),
        ("z-ai/glm-4.5-air:free", True),
        ("openai/gpt-5-mini", True),
        ("meta/llama-x-lite", True),
        ("anthropic/claude-sonnet-4.5", False),
        ("deepseek/deepseek-v4", False),
        ("", False),          # unknown/unset → no advisory (don't cry wolf)
        (None, False),
    ])
    def test_truth_table(self, model, expected):
        assert is_budget_class(model) is expected


class TestConfigIntegration:
    def _clean_env(self, monkeypatch):
        for var in ("SYSTEMU_MODEL_PRESET", "SYSTEMU_TIER1_MODEL",
                    "SYSTEMU_TIER2_MODEL", "SYSTEMU_TIER3_MODEL"):
            monkeypatch.delenv(var, raising=False)

    def test_from_env_no_preset_is_todays_defaults(self, monkeypatch):
        from sharing_on.config import Config
        self._clean_env(monkeypatch)
        cfg = Config.from_env()
        assert cfg.tier1_model == _TODAY["tier1"]
        assert cfg.tier2_model == _TODAY["tier2"]
        assert cfg.tier3_model == _TODAY["tier3"]

    def test_from_env_preset_expands_tiers(self, monkeypatch):
        from sharing_on.config import Config
        self._clean_env(monkeypatch)
        monkeypatch.setenv("SYSTEMU_MODEL_PRESET", "quality")
        cfg = Config.from_env()
        assert cfg.tier1_model == PRESETS["quality"]["tier1"]
        assert cfg.tier3_model == PRESETS["quality"]["tier3"]

    def test_explicit_tier_env_beats_preset(self, monkeypatch):
        from sharing_on.config import Config
        self._clean_env(monkeypatch)
        monkeypatch.setenv("SYSTEMU_MODEL_PRESET", "quality")
        monkeypatch.setenv("SYSTEMU_TIER1_MODEL", "my/custom-model")
        cfg = Config.from_env()
        assert cfg.tier1_model == "my/custom-model"
        assert cfg.tier2_model == PRESETS["quality"]["tier2"]


class TestSurfacesWired:
    def test_settings_page_offers_presets(self):
        from systemu.interface.pages import settings
        src = inspect.getsource(settings)
        assert "model_presets" in src, \
            "Settings must surface the preset choice next to the tier inputs"

    def test_settings_advises_on_budget_reasoning_tier(self):
        # Deviation from the plan draft: the advisory lives in Settings (where
        # the fix is), NOT the every-page health banner — that component only
        # has warning/danger severities and would nag every default install.
        # First-run discoverability comes via Wave 9's onboarding preset step.
        from systemu.interface.pages import settings
        src = inspect.getsource(settings)
        assert "is_budget_class" in src, \
            "Settings must advise when tier1 is flash/free-class"
