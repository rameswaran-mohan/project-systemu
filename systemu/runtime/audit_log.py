"""v0.9.1 audit-log helper.

Thin wrapper over ``vault.append_action_audit`` that stamps the timestamp
and normalises optional fields. Single writer for action-tool audit entries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def append_action(
    vault,
    *,
    execution_id: str,
    objective_id: int,
    action: str,
    params: Dict[str, Any],
    success: bool,
    error: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Append one action-audit entry.

    Stamps ``ts`` with the current UTC ISO timestamp. All other fields
    forwarded verbatim. ``vault.append_action_audit`` is backend-aware —
    we don't care if it lands in JSONL or a sqlite row.
    """
    entry: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "execution_id": execution_id,
        "objective_id": int(objective_id),
        "action": action,
        "params": params or {},
        "success": bool(success),
        "error": error,
    }
    if user_id is not None:
        entry["user_id"] = user_id
    vault.append_action_audit(entry)
