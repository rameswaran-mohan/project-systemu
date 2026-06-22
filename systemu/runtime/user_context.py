"""W9.2 — the ONE composer for operator identity context.

The refiner and runtime each grew their own profile handling; the quick lane
(the default path since v0.9.18) had none — the fastest lane was the most
identity-blind. This module renders the profile + office facts as a compact
instruction block any prompt can append. Empty string when there is nothing
to say — callers can append unconditionally.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def profile_context_block(vault) -> str:
    """Markdown block describing the operator, or "" when unavailable.

    Defensive: any vault error returns "" — identity context must never
    break a run.
    """
    try:
        profile = vault.get_user_profile()
    except Exception:
        logger.debug("[UserContext] profile read failed", exc_info=True)
        return ""
    if profile is None:
        return ""

    lines = ["## Operator profile (use this — do not guess identity/location)"]
    if profile.name:
        lines.append(f"- Name: {profile.name}")
    if profile.location_text:
        lines.append(
            f'- Location: "{profile.location_text}" '
            "(use this EXACT string for any location / near parameter — do not "
            "append a city/region/country or reword it)")
    if profile.timezone:
        lines.append(f"- Timezone: {profile.timezone}")
    if profile.default_output_dir:
        lines.append(f"- Deliverables folder: {profile.default_output_dir}")

    try:
        from systemu.runtime.user_profile import get_facts
        for fact in get_facts(vault, tags=["office_context"]):
            lines.append(f"- {fact.fact}")
    except Exception:
        logger.debug("[UserContext] facts read failed", exc_info=True)

    return "\n".join(lines) if len(lines) > 1 else ""
