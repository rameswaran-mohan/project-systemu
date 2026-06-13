"""W13.x — a dead/renamed model ID must not brick the whole task.

Field error (2026-06-13, balanced preset): OpenRouter 400
"deepseek/deepseek-v4 is not a valid model ID". Same dead-default class as
glm-4.5-air:free. Two fixes: (1) the runtime falls back to the shipped
budget default for the tier when the configured id is rejected — this is
the ONLY thing that auto-rescues installs whose .env already baked the dead
id; (2) the presets no longer reference unverified ids.
"""
from __future__ import annotations

import inspect


class TestInvalidModelDetection:
    def test_detects_openrouter_phrasings(self):
        from systemu.core.llm_router import _is_invalid_model_error
        for s in (
            "Error code: 400 - deepseek/deepseek-v4 is not a valid model ID",
            "invalid model",
            "model_not_found",
            "No endpoints found for foo/bar",
        ):
            assert _is_invalid_model_error(s), s

    def test_does_not_swallow_other_400s(self):
        from systemu.core.llm_router import _is_invalid_model_error
        for s in ("400 - response_format json_object not supported",
                  "rate limit exceeded", "401 unauthorized", ""):
            assert not _is_invalid_model_error(s), s


class TestTierFallback:
    def test_returns_budget_default_for_tier(self):
        from systemu.core.llm_router import _fallback_model_for_tier
        from sharing_on.model_presets import resolve_preset
        budget = resolve_preset({})
        assert _fallback_model_for_tier(1, "deepseek/deepseek-v4") == budget["tier1"]

    def test_none_when_already_the_default(self):
        from systemu.core.llm_router import _fallback_model_for_tier
        from sharing_on.model_presets import resolve_preset
        cur = resolve_preset({})["tier1"]
        assert _fallback_model_for_tier(1, cur) is None


class TestCallPathWiring:
    def test_invalid_model_checked_before_json_branch(self):
        from systemu.core import llm_router
        src = inspect.getsource(llm_router)
        # The model-id branch must appear before the JSON-mode branch in the
        # except block, else a JSON call's model-400 gets mis-handled.
        i_model = src.index("_is_invalid_model_error(exc_str)")
        i_json = src.index('"json" in response_format.get("type"')
        assert i_model < i_json


class TestPresetsHaveNoDeadIds:
    def test_no_nonflash_deepseek_v4_anywhere(self):
        from sharing_on.model_presets import PRESETS, resolve_preset
        all_models = {m for p in PRESETS.values() for m in p.values()}
        all_models |= set(resolve_preset({}).values())
        assert "deepseek/deepseek-v4" not in all_models, \
            "the dead non-flash id must be gone from every preset + default"

    def test_proven_models_only_in_budget_and_balanced(self):
        """Budget + balanced must contain ONLY ids with positive live
        evidence this cycle (quality's premium tier1 is covered by the
        runtime fallback)."""
        from sharing_on.model_presets import PRESETS
        proven = {"deepseek/deepseek-v4-flash", "google/gemini-3-flash-preview"}
        for name in ("budget", "balanced"):
            assert set(PRESETS[name].values()) <= proven, name
