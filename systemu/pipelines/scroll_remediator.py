"""Scroll remediation loop (v0.6.0-b, Stage 6).

When the scroll validator returns ``satisfiable=False`` AND emits a
``proposed_revision`` (the LLM's candidate fix for the intent mismatch),
this module orchestrates the operator-facing remediation:

1. **Surface** a side-by-side card on ``/scrolls``: original objectives
   vs. proposed objectives + the reasoning summary.  Three actions:
   *Accept revision* / *Keep original (override)* / *Workshop edit*.

2. **On Accept** — apply the revision to the Scroll, persist a record to
   ``data/scroll_remediations.jsonl``, then re-run the validator once.
   If the re-validation still fails, fall back to operator workshop
   editing (no further auto-remediation).  Hard cap: 2 cycles per scroll.

3. **On Override** — log to the audit JSONL with reason
   ``operator_override``; scroll proceeds with original objectives as-is.

4. **On Workshop** — operator handles via the existing workshop pipeline
   (``rebuild_scroll`` in ``workshop_module.py``).

The remediator is **opt-in** via the same gates as the validator
(``intelligent_supervisor_enabled`` OR ``SYSTEMU_SCROLL_VALIDATOR=1``).
The actions themselves are dispatched from the UI; this module exposes
the verbs the UI calls.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from sharing_on.config import Config
    from systemu.core.models import Scroll
    from systemu.vault.vault import Vault

from systemu.pipelines.scroll_validator import (
    ProposedRevision,
    ValidationResult,
    validate_scroll,
)

logger = logging.getLogger(__name__)

MAX_REMEDIATION_CYCLES = 2
AUDIT_FILENAME = "scroll_remediations.jsonl"


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RemediationRecord:
    """One row written to ``data/scroll_remediations.jsonl`` per cycle."""
    ts:              str
    scroll_id:       str
    cycle:           int                  # 1 or 2 (max)
    action:          str                  # accept | override | workshop
    blockers_before: List[Dict[str, Any]] = field(default_factory=list)
    proposed_revision: Optional[Dict[str, Any]] = None
    operator_note:   str = ""
    result_after:    str = ""             # "satisfiable" | "still_blocked" | "n/a"


# ─────────────────────────────────────────────────────────────────────────────
# Operator-facing verbs

def accept_revision(
    scroll: "Scroll",
    proposed: ProposedRevision,
    *,
    config: "Config",
    vault: "Vault",
    cycle: int = 1,
    operator_note: str = "",
    data_dir: Optional[Path] = None,
) -> ValidationResult:
    """Operator clicked **Accept revision** on the side-by-side card.

    Applies the proposed objectives to the scroll, re-validates ONCE, and
    returns the new ValidationResult.  Caller (UI) decides how to react
    to the second result: if still blocked, surface workshop edit instead
    of another remediation cycle.
    """
    blockers_before = _snapshot_blockers_from_scroll(scroll, config=config, vault=vault)

    _apply_revision(scroll, proposed)
    vault.save_scroll(scroll)
    logger.info(
        "[ScrollRemediator] Accepted revision for scroll %s (cycle %d) — re-validating",
        scroll.id, cycle,
    )

    if cycle > MAX_REMEDIATION_CYCLES:
        # Hard cap reached — don't re-validate; force operator workshop.
        record = RemediationRecord(
            ts=_now_iso(),
            scroll_id=scroll.id,
            cycle=cycle,
            action="accept_capped",
            blockers_before=blockers_before,
            proposed_revision=asdict(proposed),
            operator_note=operator_note,
            result_after="cap_reached",
        )
        _audit(record, data_dir=data_dir)
        return ValidationResult(
            satisfiable=False, confidence="low",
            summary=(
                f"Remediation cap reached ({MAX_REMEDIATION_CYCLES} cycles). "
                f"Use the workshop to edit the scroll directly."
            ),
        )

    re_result = validate_scroll(scroll, config=config, vault=vault)

    record = RemediationRecord(
        ts=_now_iso(),
        scroll_id=scroll.id,
        cycle=cycle,
        action="accept",
        blockers_before=blockers_before,
        proposed_revision=asdict(proposed),
        operator_note=operator_note,
        result_after="satisfiable" if re_result.satisfiable else "still_blocked",
    )
    _audit(record, data_dir=data_dir)

    return re_result


def override_revision(
    scroll: "Scroll",
    blockers: List[Dict[str, Any]],
    *,
    operator_note: str = "",
    data_dir: Optional[Path] = None,
) -> None:
    """Operator clicked **Keep original (override)**.

    Logs the override decision to the audit JSONL.  Scroll is unmodified
    and the caller is responsible for proceeding through the rest of the
    pipeline.  The blockers parameter is what was on the card at time of
    decision (audit trail — what the operator saw and chose to ignore).
    """
    record = RemediationRecord(
        ts=_now_iso(),
        scroll_id=scroll.id,
        cycle=0,
        action="override",
        blockers_before=blockers,
        proposed_revision=None,
        operator_note=operator_note,
        result_after="n/a",
    )
    _audit(record, data_dir=data_dir)
    logger.info(
        "[ScrollRemediator] Operator overrode validator for scroll %s — proceeding with original objectives",
        scroll.id,
    )


def route_to_workshop(
    scroll: "Scroll",
    blockers: List[Dict[str, Any]],
    *,
    operator_note: str = "",
    data_dir: Optional[Path] = None,
) -> None:
    """Operator clicked **Workshop edit**.

    Records the decision in the audit log; the workshop UI handles the
    edit itself via the existing ``rebuild_scroll`` pipeline.  This
    function is purely the audit hook.
    """
    record = RemediationRecord(
        ts=_now_iso(),
        scroll_id=scroll.id,
        cycle=0,
        action="workshop",
        blockers_before=blockers,
        proposed_revision=None,
        operator_note=operator_note,
        result_after="n/a",
    )
    _audit(record, data_dir=data_dir)
    logger.info(
        "[ScrollRemediator] Operator routed scroll %s to workshop for manual edit",
        scroll.id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers

def _apply_revision(scroll: "Scroll", proposed: ProposedRevision) -> None:
    """Replace ``scroll.objectives`` with the proposed revision.

    Re-uses existing objective IDs when the revision references them; new
    objectives get the next available ID.  Caller persists via
    ``vault.save_scroll``.
    """
    from systemu.core.models import Objective

    existing_ids = {getattr(o, "id", None) for o in (scroll.objectives or [])}
    next_id = max([i for i in existing_ids if isinstance(i, int)] or [0]) + 1

    new_objs: List[Objective] = []
    for entry in proposed.objectives or []:
        oid = entry.get("id")
        if oid is None:
            oid = next_id
            next_id += 1
        new_objs.append(Objective(
            id=int(oid),
            goal=str(entry.get("goal", "")),
            success_criteria=str(entry.get("success_criteria", "")),
            output_type=str(entry.get("output_type", "")),
        ))

    scroll.objectives = new_objs


def _snapshot_blockers_from_scroll(
    scroll: "Scroll", *, config, vault,
) -> List[Dict[str, Any]]:
    """Re-run the validator just to capture current blockers for the audit
    record.  Best-effort — empty list on any failure."""
    try:
        result = validate_scroll(scroll, config=config, vault=vault)
        return [asdict(b) for b in (result.blockers or [])]
    except Exception:
        return []


def _audit(record: RemediationRecord, *, data_dir: Optional[Path] = None) -> None:
    """Append a remediation record to ``data/scroll_remediations.jsonl``.

    Best-effort — never raises.  Test harnesses pass ``data_dir`` to
    redirect the audit to a tmp_path.
    """
    target = Path(data_dir or "data") / AUDIT_FILENAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), default=str) + "\n")
    except Exception:
        logger.debug("[ScrollRemediator] audit write failed", exc_info=True)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────────
# Event bus surface (UI integration)

def publish_remediation_card(
    scroll: "Scroll",
    result: ValidationResult,
) -> None:
    """Publish a side-by-side remediation card via the v0.3.6 supervisor
    flash bus.  No-op when ``result.proposed_revision`` is absent (the
    existing validator-only flash card from ``scroll_refiner.py`` covers
    the no-revision case).
    """
    if result.proposed_revision is None:
        return
    try:
        from systemu.interface.event_bus import EventBus
        bus = EventBus.get()
        bus.publish({
            "ts": _now_iso(),
            "level": "WARNING",
            "category": "approval",
            "message": f"i Scroll '{scroll.name}' has a proposed revision",
            "context": {
                "approval_message": (
                    result.summary
                    or "The validator proposed a revised objectives list."
                ) + "\n\nReview the side-by-side comparison on the Scrolls page.",
                "options": [],
                "redirect_to": "/scrolls",
                "dedup_key":   f"scroll-remediate:{scroll.id}",
                "scroll_id":   scroll.id,
                "blockers":    [asdict(b) for b in (result.blockers or [])],
                "proposed_revision": asdict(result.proposed_revision),
            },
        })
    except Exception:
        logger.debug("[ScrollRemediator] could not flash remediation card", exc_info=True)
