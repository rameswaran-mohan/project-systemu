"""Shared id→name resolver (v0.8.12).

Dashboard surfaces show human names instead of raw entity IDs. Prefix-dispatched
to the right vault.get_*; fallback-safe (returns the id on any miss, never
raises into a render); TTL-cached so repeated event-feed lookups don't hammer
the vault.

Entities with no name (exec_/sub_/dec_/notif_) are returned as-is.
Evolutions have no name → a "<target_type> evolution" summary.
"""
from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, str]] = {}
_TTL_SECONDS = 30.0


def clear_name_cache() -> None:
    """Drop all cached resolutions (used by tests + on-demand invalidation)."""
    _CACHE.clear()


def short_id(entity_id: str, length: int = 12) -> str:
    """Truncated id for the grey monospace companion line."""
    return entity_id[:length] if entity_id else ""


def _name_or_id(name: Any, entity_id: str) -> str:
    """Accept a resolved name only if it is a non-empty str; else fall back."""
    return name if isinstance(name, str) and name else entity_id


def _resolve_uncached(entity_id: str, vault: Any) -> str:
    prefix = entity_id.split("_", 1)[0] if "_" in entity_id else ""
    try:
        if prefix == "shadow":
            return _name_or_id(vault.get_shadow(entity_id).name, entity_id)
        if prefix == "scroll":
            return _name_or_id(vault.get_scroll(entity_id).name, entity_id)
        if prefix == "tool":
            return _name_or_id(vault.get_tool(entity_id).name, entity_id)
        if prefix == "skill":
            return _name_or_id(vault.get_skill(entity_id).name, entity_id)
        if prefix == "activity":
            return _name_or_id(vault.get_activity(entity_id).name, entity_id)
        if prefix in ("evolution", "evo"):
            ev = vault.get_evolution(entity_id)
            return f"{getattr(ev, 'target_entity_type', 'entity')} evolution"
    except Exception:
        # Any miss / error → fall back to the id (never break a render).
        return entity_id
    # exec_/sub_/dec_/notif_/unknown — no name exists
    return entity_id


def resolve_name(entity_id: str, vault: Any, *, max_len: int = 40) -> str:
    """Resolve an entity id to its human name, prefix-dispatched + TTL-cached.

    Falls back to the id on unknown prefix / vault miss / error. Truncates
    names longer than max_len with an ellipsis.
    """
    if not entity_id:
        return ""
    now = time.monotonic()
    hit = _CACHE.get(entity_id)
    if hit and (now - hit[0]) < _TTL_SECONDS:
        name = hit[1]
    else:
        name = _resolve_uncached(entity_id, vault)
        _CACHE[entity_id] = (now, name)
    if len(name) > max_len:
        return name[: max_len] + "…"
    return name


def resolve_names(entity_ids: List[str], vault: Any) -> List[str]:
    """Map resolve_name over a list (used by the army detail joins)."""
    return [resolve_name(e, vault) for e in (entity_ids or [])]
