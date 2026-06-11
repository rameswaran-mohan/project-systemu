"""Schedule registry — vault-persisted schedules for operator-initiated executions.

v0.8.6. One JSON file per schedule under vault/schedules/, plus an index.json
for fast list. Schedule semantics:
  - ONCE: single fire, then status → COMPLETED.
  - RECURRING: re-fires every interval_minutes; status → COMPLETED when end_at exceeded.
  - Skip-missed: if dashboard was down past a fire time, the next fire after
    restart is computed as now + interval (recurring) or once (single fire).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, TYPE_CHECKING

from systemu.core.models import Schedule, ScheduleMode, ScheduleStatus

if TYPE_CHECKING:
    from systemu.vault.vault import Vault

logger = logging.getLogger(__name__)

_MIN_INTERVAL_MINUTES = 5


def _schedules_dir(vault: "Vault") -> Path:
    """Return the directory for schedule files; create if missing."""
    root = Path(getattr(vault, "root", ".")) / "schedules"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _index_path(vault: "Vault") -> Path:
    return _schedules_dir(vault) / "index.json"


def _schedule_path(schedule_id: str, vault: "Vault") -> Path:
    return _schedules_dir(vault) / f"{schedule_id}.json"


def _save_schedule_file(sched: Schedule, vault: "Vault") -> None:
    path = _schedule_path(sched.id, vault)
    path.write_text(sched.model_dump_json(indent=2), encoding="utf-8")
    _rebuild_index_entry(sched, vault)


def _rebuild_index_entry(sched: Schedule, vault: "Vault") -> None:
    """Atomic-ish: read full index, replace entry, write back."""
    idx_path = _index_path(vault)
    entries: list = []
    if idx_path.exists():
        try:
            entries = json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception:
            entries = []
    # Remove any existing entry for this id, append fresh
    entries = [e for e in entries if e.get("id") != sched.id]
    entries.append({
        "id":           sched.id,
        "shadow_id":    sched.shadow_id,
        "scroll_id":    sched.scroll_id,
        "mode":         sched.mode.value,
        "next_fire_at": sched.next_fire_at.isoformat(),
        "status":       sched.status.value,
    })
    idx_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def create_schedule(
    *,
    shadow_id: str,
    scroll_id: str,
    mode: ScheduleMode,
    scheduled_at: datetime,
    interval_minutes: Optional[int] = None,
    end_at: Optional[datetime] = None,
    dry_run: bool = False,
    vault: "Vault",
) -> Schedule:
    """Validate inputs, persist, return the new Schedule."""
    if mode == ScheduleMode.RECURRING:
        if interval_minutes is None:
            raise ValueError("interval_minutes required for RECURRING mode")
        if interval_minutes < _MIN_INTERVAL_MINUTES:
            raise ValueError(
                f"interval_minutes must be at least {_MIN_INTERVAL_MINUTES}"
            )

    schedule_id = f"schedule_{uuid.uuid4().hex[:8]}"
    sched = Schedule(
        id=schedule_id,
        shadow_id=shadow_id,
        scroll_id=scroll_id,
        mode=mode,
        dry_run=dry_run,
        scheduled_at=scheduled_at,
        interval_minutes=interval_minutes,
        end_at=end_at,
        next_fire_at=scheduled_at,
        created_at=datetime.utcnow(),
    )
    _save_schedule_file(sched, vault)
    logger.info("[ScheduleRegistry] Created %s mode=%s next_fire=%s",
                schedule_id, mode.value, scheduled_at.isoformat())
    return sched


def get_schedule(schedule_id: str, vault: "Vault") -> Schedule:
    path = _schedule_path(schedule_id, vault)
    if not path.exists():
        raise KeyError(f"Schedule {schedule_id} not found")
    return Schedule.model_validate_json(path.read_text(encoding="utf-8"))


def list_active_schedules(vault: "Vault") -> List[Schedule]:
    """Return all ACTIVE schedules.

    Reads from index for speed; falls back to directory glob if index missing
    or out of sync.
    """
    idx_path = _index_path(vault)
    ids_to_check: list = []
    if idx_path.exists():
        try:
            entries = json.loads(idx_path.read_text(encoding="utf-8"))
            ids_to_check = [e["id"] for e in entries if e.get("status") == "active"]
        except Exception:
            ids_to_check = []
    if not ids_to_check:
        # Fallback: glob the directory
        for p in _schedules_dir(vault).glob("schedule_*.json"):
            ids_to_check.append(p.stem)

    out: List[Schedule] = []
    for sid in ids_to_check:
        try:
            s = get_schedule(sid, vault)
            if s.status == ScheduleStatus.ACTIVE:
                out.append(s)
        except Exception:
            logger.warning("[ScheduleRegistry] could not load schedule %s — skipping", sid)
    return out


def list_all_schedules(vault: "Vault", limit: int = 50) -> List[Schedule]:
    """All schedules (active + completed + cancelled), newest first."""
    out: List[Schedule] = []
    for p in sorted(_schedules_dir(vault).glob("schedule_*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(get_schedule(p.stem, vault))
        except Exception:
            continue
    return out


def cancel_schedule(schedule_id: str, vault: "Vault") -> bool:
    try:
        sched = get_schedule(schedule_id, vault)
    except KeyError:
        return False
    if sched.status != ScheduleStatus.ACTIVE:
        return False
    sched.status = ScheduleStatus.CANCELLED
    _save_schedule_file(sched, vault)
    logger.info("[ScheduleRegistry] Cancelled %s", schedule_id)
    return True


def mark_fired(schedule_id: str, now: datetime, vault: "Vault") -> None:
    """Advance the schedule after a successful fire (or fire attempt)."""
    sched = get_schedule(schedule_id, vault)
    sched.last_fire_at = now
    if sched.mode == ScheduleMode.ONCE:
        sched.status = ScheduleStatus.COMPLETED
    else:
        # Skip-missed: next fire is from NOW, not from old next_fire
        sched.next_fire_at = now + timedelta(minutes=sched.interval_minutes)
        if sched.end_at and sched.next_fire_at > sched.end_at:
            sched.status = ScheduleStatus.COMPLETED
    _save_schedule_file(sched, vault)


def mark_missed(
    schedule_id: str,
    now: datetime,
    vault: "Vault",
    advance_to: Optional[datetime] = None,
) -> None:
    """Mark a schedule as having missed a fire window (v0.8.7).

    Unlike mark_fired, this does NOT advance via skip-missed semantics —
    advance_to is computed by the caller (in _compute_next_valid_fire) based
    on the original scheduled_at + N*interval. This is the operator-friendly
    "resume from next valid slot" behavior.

    Args:
      schedule_id: target schedule
      now:         wall-clock UTC naive datetime (sets last_missed_at)
      vault:       vault instance
      advance_to:  for RECURRING, the new next_fire_at (computed by caller);
                   None for ONCE (marks COMPLETED with missed=True)
    """
    sched = get_schedule(schedule_id, vault)
    sched.last_missed_at = now
    if advance_to is None:
        # ONCE — never fires; terminal with missed flag
        sched.missed = True
        sched.status = ScheduleStatus.COMPLETED
    else:
        # RECURRING — advance + increment counter
        sched.missed_fires_count += 1
        sched.next_fire_at = advance_to
        if sched.end_at and sched.next_fire_at > sched.end_at:
            sched.status = ScheduleStatus.COMPLETED
    _save_schedule_file(sched, vault)


def delete_schedule(schedule_id: str, vault: "Vault") -> bool:
    """Hard delete (housekeeping). Returns True if removed."""
    path = _schedule_path(schedule_id, vault)
    if not path.exists():
        return False
    path.unlink()
    # Remove from index
    idx_path = _index_path(vault)
    if idx_path.exists():
        try:
            entries = json.loads(idx_path.read_text(encoding="utf-8"))
            entries = [e for e in entries if e.get("id") != schedule_id]
            idx_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        except Exception as exc:
            # The schedule file is already gone but the index still references
            # it — a dangling entry that will fail to load on next boot. Surface
            # it so the orphan is at least diagnosable.
            logger.warning(
                "[Scheduler] deleted schedule %s but failed to prune it from the "
                "index (%s); the index entry is now orphaned", schedule_id, exc)
    return True
