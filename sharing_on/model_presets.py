"""W8.1 — model-tier presets: the quality/cost tradeoff in one keystroke.

The agent's potency is capped by its reasoning model, and the shipped default
(`deepseek-v4-flash` in tier 1) is a deliberate budget choice the operator
never sees. Presets make it visible and easy WITHOUT consent problems:

  * No ``SYSTEMU_MODEL_PRESET`` env ⇒ exactly today's defaults (back-compat).
  * Explicit ``SYSTEMU_TIER{1,2,3}_MODEL`` overrides ALWAYS beat the preset
    (Config.from_env applies them on top of the resolved preset).
  * Preset names expand to editable strings — the Settings tier inputs remain
    the escape hatch if a catalog name drifts.

Lives in ``sharing_on`` (not ``systemu``) because Config consumes it and the
import direction is systemu → sharing_on, never the reverse.
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional

# Today's shipped defaults — "budget" IS the no-preset behaviour.
_BUDGET = {
    "tier1": "deepseek/deepseek-v4-flash",   # deep reasoning
    "tier2": "deepseek/deepseek-v4-flash",   # structured / code
    # W11.7: was z-ai/glm-4.5-air:free — OpenRouter now 404s it ("This model
    # is unavailable"), which silently killed every tier-3 consumer
    # (web_extract et al.) on default installs (field telemetry 2026-06-11).
    # deepseek-v4-flash is cheap and proven live in the field.
    "tier3": "deepseek/deepseek-v4-flash",   # fast / formatting
}

# W13.x: `deepseek/deepseek-v4` (non-flash) was a DEAD id — OpenRouter
# returns 400 "not a valid model ID" (field error 2026-06-13, balanced
# preset tier1). Same dead-default class as glm-4.5-air:free. Presets now
# use only ids with positive live evidence this cycle:
#   * deepseek/deepseek-v4-flash       — the budget default, proven in field
#   * google/gemini-3-flash-preview    — drove the entire A2 recording E2E
#     (via OpenRouter, key-aware routing) — a stronger flash-class brain
# quality tier1 keeps the premium Anthropic opt-in; the runtime fallback
# (_is_invalid_model_error → budget default) covers any id that still drifts.
PRESETS: Dict[str, Dict[str, str]] = {
    "quality": {
        "tier1": "anthropic/claude-sonnet-4.5",
        "tier2": "google/gemini-3-flash-preview",
        "tier3": "deepseek/deepseek-v4-flash",
    },
    "balanced": {
        "tier1": "google/gemini-3-flash-preview",
        "tier2": "deepseek/deepseek-v4-flash",
        "tier3": "deepseek/deepseek-v4-flash",
    },
    "budget": dict(_BUDGET),
}

# Name fragments that mark a model as speed/cost-optimized — good for tier 3,
# a potency cap as the tier-1 reasoning brain.
_BUDGET_MARKERS = (":free", "-flash", "-air", "-mini", "-lite", "-nano", "-tiny")


def resolve_preset(env: Mapping[str, str]) -> Dict[str, str]:
    """Resolve the tier-model defaults for the given environment.

    Returns a fresh dict {tier1, tier2, tier3}. Unknown or missing preset
    names fall back to the budget defaults — never raises.
    """
    name = (env.get("SYSTEMU_MODEL_PRESET") or "").strip().lower()
    return dict(PRESETS.get(name, _BUDGET))


def is_budget_class(model: Optional[str]) -> bool:
    """True when *model* is recognizably a flash/free/mini-class model.

    Empty/unknown names return False — the advisory must not cry wolf over
    a model it can't classify.
    """
    if not model:
        return False
    lowered = str(model).lower()
    return any(marker in lowered for marker in _BUDGET_MARKERS)


# R-A10 B11 (DEC-20a) — MODEL-MATRIX `locality` classifier.
#
# A pinned static id-prefix map: which deployment locality class a model id
# belongs to. Vocabulary (docs/MODEL-MATRIX.md):
#   * local_capable  — a local `ollama/*` model can serve this stage now/soon.
#   * cloud_required — needs a frontier cloud model (e.g. claude-sonnet-*);
#                      no local model qualifies.
#   * cloud_default  — runs cloud today (flash-class + the catch-all); a local
#                      model may qualify later per its fixtures.
# NOTHING consumes this at runtime yet — it rides the artifact for R-P3b's
# privacy page and future PCM (DEC-20b). It is a pinned constant, not a
# hot-path classifier. Order matters: the first matching prefix/marker wins.
_LOCALITY_PREFIXES = (
    ("ollama/", "local_capable"),
    ("anthropic/claude-sonnet", "cloud_required"),
)
_LOCALITY_DEFAULT = "cloud_default"


def locality_of(model_id: str) -> str:
    """Classify *model_id* into its MODEL-MATRIX locality class (DEC-20a).

    `ollama/*` ⇒ local_capable; `anthropic/claude-sonnet*` ⇒ cloud_required;
    flash-class (`*-flash*`) and everything else (incl. empty/unknown) ⇒
    cloud_default. Never raises — an unclassifiable id is `cloud_default`.
    """
    lowered = str(model_id or "").lower()
    for prefix, locality in _LOCALITY_PREFIXES:
        if lowered.startswith(prefix):
            return locality
    if "-flash" in lowered:
        return "cloud_default"
    return _LOCALITY_DEFAULT
