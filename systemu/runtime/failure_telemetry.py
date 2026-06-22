"""Failure telemetry — record every Shadow failure with structured fields.

v0.4.0-0 foundation. Before we build the Intelligent Supervisor, we need
to know what's actually failing. This module captures every recoverable
and unrecoverable failure event into ``data/failure_telemetry.jsonl`` so
we can produce a failure-mode histogram and ground later design work
against real data instead of imagined scenarios.

What gets recorded:

* **tool_failure**       — a tool call returned ``success=False`` (per call)
* **execution_terminal** — a Shadow execution reached terminal state with
                           status in {failure, partial, cancelled, stuck}
* **supervisor_diagnosis** — the existing post-mortem ``_analyze_failure``
                             output, mirrored here so all failure data is
                             queryable from a single file

The file is JSONL with one row per event.  It rotates when the file
exceeds ``MAX_BYTES`` (5 MB) by renaming to ``failure_telemetry.jsonl.1``
— rotation is a single rename, fast and atomic.

Inspection helpers:

* :func:`load_events`           — generator over events on disk
* :func:`compute_histogram`     — group-and-count by configurable fields
* CLI: ``sharing_on debug failure-histogram``

Importantly: this module **never** raises into the caller path.  All
telemetry write failures are swallowed and logged; the shadow / supervisor
must never crash because telemetry is broken.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Storage location

_DEFAULT_FILENAME = "failure_telemetry.jsonl"
_DATA_DIR = Path("data")
_MAX_BYTES = 5 * 1024 * 1024   # 5 MB rotate threshold
_WRITE_LOCK = threading.Lock()


def _default_path() -> Path:
    return _DATA_DIR / _DEFAULT_FILENAME


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# Event schema

@dataclass
class FailureEvent:
    """One structured row in the telemetry file.

    Fields are deliberately broad so we don't have to re-instrument when
    new failure dimensions emerge.  ``context`` is the bag for
    domain-specific fields (tool params, exit codes, error tails).
    """
    ts:            str
    event_type:    str               # tool_failure | execution_terminal | supervisor_diagnosis
    shadow_id:     Optional[str] = None
    execution_id:  Optional[str] = None
    activity_id:   Optional[str] = None
    scroll_id:     Optional[str] = None
    tool_name:     Optional[str] = None
    error_type:    Optional[str] = None     # missing_dependency, param_error, etc.
    status:        Optional[str] = None     # for execution_terminal: failure/partial/cancelled/stuck
    failure_category: Optional[str] = None  # for supervisor_diagnosis
    error:         Optional[str] = None
    context:       Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Writers

def record(event: FailureEvent, *, path: Optional[Path] = None) -> None:
    """Append a single event to the telemetry file.

    Never raises into the caller — every IO error is caught + logged.
    Thread-safe via a module-level lock around the append + rotation
    check.  The lock is cheap because writes are infrequent (failure
    events, not every tool call).
    """
    target = path or _default_path()
    try:
        with _WRITE_LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            _maybe_rotate(target)
            line = json.dumps(asdict(event), ensure_ascii=False)
            with target.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        logger.debug(
            "[FailureTelemetry] could not record event %s — telemetry is best-effort",
            event.event_type, exc_info=True,
        )


def record_tool_failure(
    *,
    shadow_id: Optional[str],
    execution_id: Optional[str],
    tool_name: str,
    error_type: Optional[str],
    error: Optional[str],
    activity_id: Optional[str] = None,
    scroll_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Convenience publisher for individual tool-call failures."""
    record(FailureEvent(
        ts=_now_iso(),
        event_type="tool_failure",
        shadow_id=shadow_id,
        execution_id=execution_id,
        activity_id=activity_id,
        scroll_id=scroll_id,
        tool_name=tool_name,
        error_type=error_type,
        error=(error or "")[:1000],
        context=extra or {},
    ))


def record_execution_terminal(
    *,
    shadow_id: Optional[str],
    execution_id: Optional[str],
    activity_id: Optional[str],
    scroll_id: Optional[str],
    status: str,
    iterations: int,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Publish on shadow execution terminal — only when status != success."""
    if status == "success":
        return
    record(FailureEvent(
        ts=_now_iso(),
        event_type="execution_terminal",
        shadow_id=shadow_id,
        execution_id=execution_id,
        activity_id=activity_id,
        scroll_id=scroll_id,
        status=status,
        context={"iterations": iterations, **(extra or {})},
    ))


def record_supervisor_diagnosis(
    *,
    shadow_id: Optional[str],
    activity_id: Optional[str],
    diagnosis: Dict[str, Any],
) -> None:
    """Mirror the supervisor's post-mortem diagnosis into telemetry."""
    record(FailureEvent(
        ts=_now_iso(),
        event_type="supervisor_diagnosis",
        shadow_id=shadow_id,
        activity_id=activity_id,
        failure_category=diagnosis.get("failure_category"),
        error=diagnosis.get("root_cause", "")[:1000] if diagnosis.get("root_cause") else None,
        context={
            "immediate_fix":     diagnosis.get("immediate_fix"),
            "prevention":        diagnosis.get("prevention"),
            "retry_recommended": diagnosis.get("retry_recommended"),
        },
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Rotation

def _maybe_rotate(target: Path) -> None:
    """If the file exceeds ``_MAX_BYTES``, rename to ``.1`` and start fresh.

    Single-generation rotation: ``.1`` is overwritten on each rotation.
    Adequate for telemetry — we keep recent failures, older ones drop off.
    The user can compress / archive ``.1`` externally if longer retention
    is needed.
    """
    if not target.exists():
        return
    try:
        if target.stat().st_size <= _MAX_BYTES:
            return
    except OSError:
        return
    backup = target.with_suffix(target.suffix + ".1")
    try:
        if backup.exists():
            backup.unlink()
        os.replace(target, backup)
        logger.info("[FailureTelemetry] rotated %s → %s", target.name, backup.name)
    except Exception:
        logger.debug("[FailureTelemetry] rotation failed", exc_info=True)


# ─────────────────────────────────────────────────────────────────────────────
# Readers + analytics

def load_events(path: Optional[Path] = None) -> Iterator[FailureEvent]:
    """Yield events from the telemetry file.

    Missing file → empty iterator.  Malformed lines are skipped with a
    debug log — telemetry data should never crash the analyzer.
    """
    target = path or _default_path()
    if not target.exists():
        return
    try:
        with target.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                    yield FailureEvent(**data)
                except Exception:
                    logger.debug("[FailureTelemetry] skipping malformed line")
                    continue
    except Exception:
        logger.debug("[FailureTelemetry] could not read %s", target, exc_info=True)


def compute_histogram(
    *,
    group_by: Iterable[str] = ("event_type", "error_type", "tool_name"),
    event_types: Optional[Iterable[str]] = None,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Group telemetry events by configurable keys and return counts.

    Args:
        group_by:    Field names to bucket on.  Missing fields show as "".
        event_types: Restrict to specified event_types; None means all.
        path:        Telemetry file (defaults to ``data/failure_telemetry.jsonl``).

    Returns rows of the form: ``{"key1": "...", "key2": "...", "count": N}``.
    Sorted by count descending.
    """
    keys = tuple(group_by)
    filter_types = set(event_types) if event_types else None
    counter: Counter = Counter()
    for ev in load_events(path):
        if filter_types and ev.event_type not in filter_types:
            continue
        bucket = tuple((getattr(ev, k, "") or "") for k in keys)
        counter[bucket] += 1
    rows: List[Dict[str, Any]] = []
    for bucket, count in counter.most_common():
        row = dict(zip(keys, bucket))
        row["count"] = count
        rows.append(row)
    return rows
