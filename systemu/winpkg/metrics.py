"""E2 — local-only first-run metric instrumentation (SPEC §14 E2; the AC1 metric).

Records two timestamps and subtracts them: install finished, first task
completed. The result is the "time-to-first-completed-task" number E2 is
measured on, shown to the operator on completion ("you were set up in 11 min").

**No phone-home, in any mode.** This module writes one JSON file inside the
install root and does nothing else — it imports no HTTP client and has no
transport seam a later change could quietly fill in. Partners report the number
manually; that is deliberate, and it is the local-first promise in miniature.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_INSTALLED_AT = "installed_at"
_FIRST_TASK_AT = "first_task_completed_at"
_VERSION = "version"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class FirstRunMetrics:
    """Reads/writes the install marker. Tolerant of a missing or corrupt file —
    a broken metric must never block an install or a task."""

    def __init__(self, marker_file: Path):
        self.marker_file = Path(marker_file)

    # -- storage ------------------------------------------------------------

    def _read(self) -> dict:
        try:
            data = json.loads(self.marker_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self.marker_file.parent.mkdir(parents=True, exist_ok=True)
        self.marker_file.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )

    # -- stamps -------------------------------------------------------------

    def stamp_installed(self, *, version: str, when: Optional[datetime] = None) -> None:
        data = self._read()
        data[_INSTALLED_AT] = (when or _utcnow()).isoformat()
        data[_VERSION] = version
        self._write(data)

    def stamp_first_task_completed(self, *, when: Optional[datetime] = None) -> bool:
        """Record the first completed task. Idempotent: a later task never
        overwrites the first one (the metric is time-to-*first*).

        Returns True if this call was the one that recorded it.
        """
        data = self._read()
        if data.get(_FIRST_TASK_AT):
            return False
        data[_FIRST_TASK_AT] = (when or _utcnow()).isoformat()
        self._write(data)
        return True

    # -- the metric ---------------------------------------------------------

    def seconds_to_first_completed_task(self) -> Optional[float]:
        data = self._read()
        installed = _parse(data.get(_INSTALLED_AT))
        completed = _parse(data.get(_FIRST_TASK_AT))
        if installed is None or completed is None:
            return None
        delta = (completed - installed).total_seconds()
        return delta if delta >= 0 else None

    def human_summary(self) -> Optional[str]:
        """The line shown on completion, or None when the metric isn't known."""
        seconds = self.seconds_to_first_completed_task()
        if seconds is None:
            return None
        minutes = int(seconds // 60)
        if minutes < 1:
            return "you were set up in under a minute"
        if minutes == 1:
            return "you were set up in 1 min"
        return f"you were set up in {minutes} min"
