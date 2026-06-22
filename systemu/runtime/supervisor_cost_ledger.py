"""Supervisor cost ledger (v0.4.0-f).

Tracks per-hour and per-day LLM spend for the Intelligent Supervisor and
trips an auto-disable kill switch when caps are breached.  Persists to
``data/supervisor_cost_ledger.json`` so it survives daemon restart.

Used by :class:`ExecutionMind`:

* Before each LLM call: ``can_spend(estimated_usd)`` — returns False when
  the call would exceed the per-hour or per-day cap.  ExecutionMind then
  returns ``DO_NOTHING`` with a budget-exhausted rationale.
* After each LLM call: ``record(usd)`` — adds the spend to both buckets.

Bucket semantics:

* **Per-hour bucket** rolls over each hour.  An hour with cost > cap
  trips ``hour_disabled_until`` to the start of the next hour.
* **Per-day bucket** rolls over each UTC day.  A day with cost > cap
  trips ``day_disabled_until`` to midnight UTC.
* When either disabled-until is in the future, ``can_spend`` returns
  False regardless of current spend.

The kill switch is **read-only** for ExecutionMind: it never re-enables
itself automatically.  Operator must call ``reset_kill_switch()`` to
clear ``day_disabled_until`` (the per-hour rolls over naturally).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_LEDGER_PATH = Path("data") / "supervisor_cost_ledger.json"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _day_floor(dt: datetime) -> datetime:
    return datetime.combine(dt.date(), time.min, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CostState:
    hour_start_iso:        str = ""
    hour_spent_usd:        float = 0.0
    day_start_iso:         str = ""
    day_spent_usd:         float = 0.0
    hour_disabled_until:   str = ""    # ISO timestamp; empty when not tripped
    day_disabled_until:    str = ""
    total_spent_usd:       float = 0.0
    total_calls_recorded:  int = 0


class SupervisorCostLedger:
    """Thread-safe rolling ledger with hour + day caps.

    Args:
        path:        On-disk ledger file.
        max_per_hour_usd: Per-hour cap.  Set to 0 to disable the hour check.
        max_per_day_usd:  Per-day cap.  Set to 0 to disable the day check.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        max_per_hour_usd: float = 5.0,
        max_per_day_usd:  float = 50.0,
    ):
        self.path = Path(path or _DEFAULT_LEDGER_PATH)
        self.max_per_hour = float(max_per_hour_usd)
        self.max_per_day  = float(max_per_day_usd)
        self._lock = threading.Lock()
        self._state = self._load()

    # ── Public API ────────────────────────────────────────────────────────

    def can_spend(self, estimated_usd: float = 0.0) -> bool:
        """Return True when an LLM call costing ``estimated_usd`` is allowed."""
        with self._lock:
            self._roll_over_locked()
            if not self._allowed_locked():
                return False
            if self.max_per_hour and self._state.hour_spent_usd + estimated_usd > self.max_per_hour:
                self._trip_hour_locked()
                return False
            if self.max_per_day and self._state.day_spent_usd + estimated_usd > self.max_per_day:
                self._trip_day_locked()
                return False
            return True

    def record(self, usd: float) -> None:
        """Add spend to the rolling buckets and persist."""
        with self._lock:
            self._roll_over_locked()
            self._state.hour_spent_usd += float(usd)
            self._state.day_spent_usd  += float(usd)
            self._state.total_spent_usd += float(usd)
            self._state.total_calls_recorded += 1
            # Trip kill switch on breach so the NEXT call sees the disable
            # even if estimated_usd is left at 0 (default).
            if self.max_per_hour and self._state.hour_spent_usd > self.max_per_hour:
                self._trip_hour_locked()
            if self.max_per_day and self._state.day_spent_usd > self.max_per_day:
                self._trip_day_locked()
            self._save_locked()

    def reset_kill_switch(self) -> None:
        """Operator-driven reset of the day-level disable.

        Clears the day + hour disables AND resets the rolling counters
        so future ``can_spend`` calls don't immediately re-trip on the
        unchanged spent-vs-cap math.  ``total_spent_usd`` is preserved
        for audit — only the rolling buckets are cleared.
        """
        with self._lock:
            if self._state.day_disabled_until or self._state.hour_disabled_until:
                logger.info(
                    "[CostLedger] Operator reset of kill switch — "
                    "buckets cleared (total_spent_usd=%.2f preserved)",
                    self._state.total_spent_usd,
                )
            self._state.day_disabled_until = ""
            self._state.hour_disabled_until = ""
            self._state.hour_spent_usd = 0.0
            self._state.day_spent_usd = 0.0
            self._save_locked()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._roll_over_locked()
            return asdict(self._state)

    # ── Internals ─────────────────────────────────────────────────────────

    def _allowed_locked(self) -> bool:
        now = _now().isoformat(timespec="seconds")
        if self._state.day_disabled_until and self._state.day_disabled_until > now:
            return False
        if self._state.hour_disabled_until and self._state.hour_disabled_until > now:
            return False
        return True

    def _roll_over_locked(self) -> None:
        now = _now()
        current_hour = _hour_floor(now).isoformat(timespec="seconds")
        current_day  = _day_floor(now).isoformat(timespec="seconds")
        if self._state.hour_start_iso != current_hour:
            self._state.hour_start_iso = current_hour
            self._state.hour_spent_usd = 0.0
            # Clear hour-trip if it has elapsed
            if self._state.hour_disabled_until and self._state.hour_disabled_until <= now.isoformat(timespec="seconds"):
                self._state.hour_disabled_until = ""
        if self._state.day_start_iso != current_day:
            self._state.day_start_iso = current_day
            self._state.day_spent_usd = 0.0

    def _trip_hour_locked(self) -> None:
        next_hour = _hour_floor(_now()) + timedelta(hours=1)
        self._state.hour_disabled_until = next_hour.isoformat(timespec="seconds")
        logger.warning(
            "[CostLedger] Per-hour cap breached ($%.2f > $%.2f) — supervisor disabled until %s",
            self._state.hour_spent_usd, self.max_per_hour, self._state.hour_disabled_until,
        )

    def _trip_day_locked(self) -> None:
        next_day = _day_floor(_now()) + timedelta(days=1)
        self._state.day_disabled_until = next_day.isoformat(timespec="seconds")
        logger.warning(
            "[CostLedger] Per-day cap breached ($%.2f > $%.2f) — supervisor disabled until %s",
            self._state.day_spent_usd, self.max_per_day, self._state.day_disabled_until,
        )

    def _load(self) -> CostState:
        if not self.path.exists():
            now = _now()
            return CostState(
                hour_start_iso=_hour_floor(now).isoformat(timespec="seconds"),
                day_start_iso=_day_floor(now).isoformat(timespec="seconds"),
            )
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state = CostState(**{
                    k: data.get(k, getattr(CostState(), k))
                    for k in CostState.__dataclass_fields__
                })
                return state
        except Exception:
            logger.exception("[CostLedger] Could not load %s — starting fresh", self.path)
        now = _now()
        return CostState(
            hour_start_iso=_hour_floor(now).isoformat(timespec="seconds"),
            day_start_iso=_day_floor(now).isoformat(timespec="seconds"),
        )

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(asdict(self._state), indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception:
            logger.exception("[CostLedger] Could not persist ledger to %s", self.path)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton — initialised lazily by ExecutionMind so tests can
# inject a different instance.

_singleton: Optional[SupervisorCostLedger] = None
_singleton_lock = threading.Lock()


def get_ledger(config=None, *, force_path: Optional[Path] = None) -> SupervisorCostLedger:
    """Return the process-wide ledger, lazily constructed.

    Reads caps from ``config.supervisor_llm_budget_per_hour_usd`` /
    ``supervisor_llm_budget_per_day_usd`` when supplied.  Tests can pass
    ``force_path`` to bind a fresh ledger to a tmp file.
    """
    global _singleton
    with _singleton_lock:
        if force_path is not None:
            inst = SupervisorCostLedger(
                path=force_path,
                max_per_hour_usd=(config and getattr(config, "supervisor_llm_budget_per_hour_usd", 5.0)) or 5.0,
                max_per_day_usd=(config and getattr(config, "supervisor_llm_budget_per_day_usd",  50.0)) or 50.0,
            )
            return inst
        if _singleton is None:
            _singleton = SupervisorCostLedger(
                max_per_hour_usd=(config and getattr(config, "supervisor_llm_budget_per_hour_usd", 5.0)) or 5.0,
                max_per_day_usd=(config and getattr(config, "supervisor_llm_budget_per_day_usd",  50.0)) or 50.0,
            )
        return _singleton


def reset_singleton_for_tests() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None
