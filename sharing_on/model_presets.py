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

PRESETS: Dict[str, Dict[str, str]] = {
    "quality": {
        "tier1": "anthropic/claude-sonnet-4.5",
        "tier2": "deepseek/deepseek-v4",
        "tier3": "deepseek/deepseek-v4-flash",
    },
    "balanced": {
        "tier1": "deepseek/deepseek-v4",
        "tier2": "deepseek/deepseek-v4-flash",
        "tier3": "deepseek/deepseek-v4-flash",   # W11.7: glm-4.5-air:free 404s
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
