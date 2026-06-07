"""v0.9.6 L7 inactivity-triggered curator state + idle-check infrastructure.

Hermes pattern: NOT a cron daemon. When the system is idle and the last
curator review was longer than ``interval_hours`` ago, the scheduler calls
``should_run()`` to decide whether to spawn a forked review agent.

This module ships:
- ``.curator_state`` JSON file format (last_run_at, run_count, paused, summary)
- ``should_run(vault_root, config)`` idle-check + interval gate
- ``mark_run_complete(vault_root, summary)`` post-run state update
- ``pause/resume`` operator controls

The actual review-fork (forked Tier-1 LLM agent that pins/archives/
consolidates skills) wires in via Task 5 (pin/archive lifecycle).

Strict invariants (carry over from Hermes):
- Never auto-delete; only archive (recoverable)
- Pinned items bypass all auto-transitions
- Curator uses auxiliary client — never touches main session's prompt cache
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)


def default_state() -> Dict[str, Any]:
    return {
        "last_run_at": None,
        "last_run_summary": None,
        "last_run_duration_seconds": None,
        "paused": False,
        "run_count": 0,
    }


def _state_file(vault_root: Union[str, Path]) -> Path:
    return Path(vault_root) / "skills" / ".curator_state"


def load_state(vault_root: Union[str, Path]) -> Dict[str, Any]:
    """Load curator state from .curator_state file. Returns default state
    if the file doesn't exist or is malformed."""
    p = _state_file(vault_root)
    if not p.exists():
        return default_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_state()
        # Merge defaults so old states get new fields
        merged = default_state()
        merged.update(data)
        return merged
    except Exception as exc:
        logger.warning("[Curator] corrupt state file at %s: %s — using defaults", p, exc)
        return default_state()


def save_state(vault_root: Union[str, Path], state: Dict[str, Any]) -> None:
    """Atomically write the curator state file via tempfile + rename."""
    p = _state_file(vault_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".curator_state.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, default=str)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def should_run(vault_root: Union[str, Path], config) -> bool:
    """Return True when the curator should run a review pass NOW.

    Checks (in order):
    1. config.curator_enabled is True
    2. state.paused is False
    3. last_run_at is None OR (now - last_run_at) >= curator_interval_hours
    """
    if not getattr(config, "curator_enabled", True):
        return False
    state = load_state(vault_root)
    if state.get("paused"):
        return False
    last_ts = _parse_ts(state.get("last_run_at"))
    if last_ts is None:
        return True  # never run before
    interval = timedelta(hours=int(getattr(config, "curator_interval_hours", 168)))
    return datetime.now(timezone.utc) - last_ts >= interval


def mark_run_complete(
    vault_root: Union[str, Path],
    *,
    summary: str = "",
    duration_seconds: Optional[float] = None,
) -> None:
    """Record that a curator pass completed. Updates last_run_at + count."""
    state = load_state(vault_root)
    state["last_run_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    state["last_run_summary"] = summary[:500]
    if duration_seconds is not None:
        state["last_run_duration_seconds"] = float(duration_seconds)
    state["run_count"] = int(state.get("run_count", 0)) + 1
    save_state(vault_root, state)


def pause(vault_root: Union[str, Path]) -> None:
    state = load_state(vault_root)
    state["paused"] = True
    save_state(vault_root, state)


def resume(vault_root: Union[str, Path]) -> None:
    state = load_state(vault_root)
    state["paused"] = False
    save_state(vault_root, state)
