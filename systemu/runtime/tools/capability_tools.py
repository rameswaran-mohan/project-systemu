"""v0.9.3 capability LLM tools — surface the capability ledger to the agent."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from systemu.runtime import capability_ledger


def _to_lightweight(cap) -> Dict[str, Any]:
    return {
        "name": cap.name,
        "kind": cap.kind,
        "invocations": cap.invocations,
        "last_used_at": cap.last_used_at.isoformat() if cap.last_used_at else None,
    }


def capability_list_my_capabilities(*, vault, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """List registered capabilities. Optionally filter by kind ('tool' or 'skill')."""
    return [_to_lightweight(c) for c in capability_ledger.list_capabilities(vault, kind=kind)]


def capability_get_stats(*, vault, name: str) -> Optional[Dict[str, Any]]:
    """Return rolled-up usage stats for one capability — or None if not registered."""
    return capability_ledger.get_stats(vault, name)


def capability_last_used(*, vault, name: str) -> Optional[str]:
    """Return ISO timestamp of last invocation, or None if never used / not registered."""
    cap = capability_ledger.get_capability(vault, name)
    if cap is None or cap.last_used_at is None:
        return None
    return cap.last_used_at.isoformat()
