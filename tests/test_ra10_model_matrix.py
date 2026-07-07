"""R-A10 step B11 — MODEL-MATRIX tier config (planner/binder/parser) + locality.

These pin the DEC-12 MODEL-MATRIX artifact (docs/MODEL-MATRIX.md) into code:
  * three per-stage tier knobs on Config, mirroring `supervisor_tier_routine`'s
    string-field shape and resolving via the `execution_mind.py:447` idiom
    (`1 if "1" in label else (3 if "3" in label else 2)`) to planner=1,
    binder=1, parser=3;
  * a static `locality_of(model_id)` classifier (DEC-20a) — a pinned constant
    with no runtime consumer yet.
"""
from __future__ import annotations

import os

from sharing_on.config import Config
from sharing_on.model_presets import locality_of


def _resolve_tier(label: str) -> int:
    """The one string->int idiom (execution_mind.py:447), mirrored."""
    return 1 if "1" in label else (3 if "3" in label else 2)


# ---------------------------------------------------------------------------
# per-stage tier knobs
# ---------------------------------------------------------------------------

def test_default_config_exposes_per_stage_tier_fields():
    cfg = Config()
    assert hasattr(cfg, "planner_tier")
    assert hasattr(cfg, "binder_tier")
    assert hasattr(cfg, "parser_tier")


def test_default_tiers_resolve_to_planner1_binder1_parser3():
    cfg = Config()
    assert _resolve_tier(cfg.planner_tier) == 1
    assert _resolve_tier(cfg.binder_tier) == 1
    assert _resolve_tier(cfg.parser_tier) == 3


def test_from_env_defaults_match_matrix():
    # from_env with a clean environment yields the same MODEL-MATRIX defaults.
    saved = {
        k: os.environ.pop(k)
        for k in ("SYSTEMU_PLANNER_TIER", "SYSTEMU_BINDER_TIER", "SYSTEMU_PARSER_TIER")
        if k in os.environ
    }
    try:
        cfg = Config.from_env()
        assert _resolve_tier(cfg.planner_tier) == 1
        assert _resolve_tier(cfg.binder_tier) == 1
        assert _resolve_tier(cfg.parser_tier) == 3
    finally:
        os.environ.update(saved)


def test_from_env_reads_tier_overrides():
    saved = {
        k: os.environ.get(k)
        for k in ("SYSTEMU_PLANNER_TIER", "SYSTEMU_BINDER_TIER", "SYSTEMU_PARSER_TIER")
    }
    os.environ["SYSTEMU_PLANNER_TIER"] = "tier3"
    os.environ["SYSTEMU_BINDER_TIER"] = "tier2"
    os.environ["SYSTEMU_PARSER_TIER"] = "tier1"
    try:
        cfg = Config.from_env()
        assert _resolve_tier(cfg.planner_tier) == 3
        assert _resolve_tier(cfg.binder_tier) == 2
        assert _resolve_tier(cfg.parser_tier) == 1
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# locality_of (DEC-20a)
# ---------------------------------------------------------------------------

def test_locality_ollama_is_local_capable():
    assert locality_of("ollama/llama3") == "local_capable"


def test_locality_claude_sonnet_is_cloud_required():
    assert locality_of("anthropic/claude-sonnet-4.5") == "cloud_required"


def test_locality_flash_is_cloud_default():
    assert locality_of("google/gemini-3-flash-preview") == "cloud_default"


def test_locality_unknown_defaults_to_cloud_default():
    assert locality_of("some/unknown-model") == "cloud_default"
    assert locality_of("") == "cloud_default"
