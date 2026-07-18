"""R-W1 (W-A slice-2a) — project the SituationReport into the world-model fact store.

The FIRST wiring of the slice-1 substrate into the live run: after the §5.1
situational-inventory survey builds its report, this WRITE-ONLY populator projects
each entry into the durable ``FactStore``, so the world model is non-empty from the
operator's actual setup (visible via ``sharing-on world`` / ``world.query``).

STRICTLY ADDITIVE + STORE-WRITE-ONLY — the slice-2a boundary:
  * it NEVER mutates the report or ``context._situation_report`` — the open-world
    planner's input is byte-identical (no planner-input change);
  * NO bind source reads the store — the §5.3 binder is untouched, so a fact written
    here can NEVER seed a silent bind. The AC1 binder assertion (content_derived can't
    silent-bind, read FROM the store) is a later, 4-lens-gated slice. Today the store
    is a read-only OBSERVABILITY surface.
  * FAIL-SAFE: it runs inside the survey's swallow-all try/except AND is itself
    defensive per-entry — a malformed entry is skipped, never breaking the survey.

Provenance vs. bind-taint (the load-bearing distinction for slice-2b): each entry
already carries a valid ``ORIGIN_CLASSES`` origin_class set by the inventory builder
from the SOURCE KIND (not from forgeable content), copied verbatim as the Fact's
honest PROVENANCE (who asserted it). This is NOT a bind-taint clearance. When a future
slice teaches the binder to read the store, it MUST re-derive conservative bind-taint
(as ``requirement_binder._entry_origin`` already does — always ``content_derived`` for
an inventory value), never trusting this field for a silent bind.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List

from systemu.runtime.world_model import Fact, FactStore, ProvStep, fact_id_for

logger = logging.getLogger(__name__)

#: A single run's contribution of file-derived facts is bounded. `data_location` is the
#: churny kind (a busy root re-mints path facts as files come and go), and slice-2a has
#: no removal yet — belief-revision + the gardener (WM-3/WM-13) are W-D. This caps what
#: ONE run can add; cross-run pruning is the gardener's job (documented, not silently
#: unbounded).
_MAX_DATA_LOCATION_PER_RUN = 200


def _facts_from_report(report: Any) -> List[Fact]:
    """Map SituationReport entries → world-model Facts. Pure; per-entry defensive — a
    malformed entry (e.g. an out-of-vocab origin_class the Fact validator rejects) is
    skipped, not raised. ``value``/``ref`` are ids/names/paths only, never secrets."""
    now = datetime.now(timezone.utc).isoformat()
    facts: List[Fact] = []

    def _add(kind: str, value: Any, origin_class: Any, ref: Any) -> None:
        if value is None or str(value) == "":
            return
        try:
            facts.append(Fact(
                fact_id=fact_id_for(kind, value),
                kind=kind, value=value, origin_class=str(origin_class),
                confidence=1.0,             # a surveyed fact is directly observed
                last_confirmed=now,
                # ONE stable inventory provenance step (deduped by (source_kind, ref)
                # in put_fact, so re-observing across runs never grows the chain).
                source_chain=[ProvStep(source_kind="inventory", ref=str(ref or ""), at=now)],
            ))
        except Exception:
            logger.debug("[world-model] skipped a malformed inventory fact (%s)", kind, exc_info=True)

    for svc in getattr(report, "services", None) or []:
        _add("service", getattr(svc, "name", None),
             getattr(svc, "origin_class", "operator"), getattr(svc, "name", None))
    for cap in getattr(report, "capabilities", None) or []:
        _add("capability", getattr(cap, "tool_id", None),
             getattr(cap, "origin_class", "systemu_authored"), getattr(cap, "tool_id", None))
    n_data = 0
    for root in getattr(report, "roots", None) or []:
        for fh in getattr(root, "salient", None) or []:
            if n_data >= _MAX_DATA_LOCATION_PER_RUN:
                break
            _add("data_location", getattr(fh, "path", None),
                 getattr(fh, "origin_class", "content_derived"), getattr(fh, "path", None))
            n_data += 1
        if n_data >= _MAX_DATA_LOCATION_PER_RUN:
            break
    for name in getattr(report, "credentials", None) or []:
        # credentials are service NAMES only (never a secret value) — operator-held.
        _add("credential_ref", name, "operator", name)

    return facts


def populate_from_situation(report: Any, vault: Any) -> int:
    """Project ``report`` into ``FactStore(vault)``. Returns the number of facts
    written. WRITE-ONLY + FAIL-SAFE — never raises (a failure returns 0)."""
    try:
        facts = _facts_from_report(report)
        if not facts:
            return 0
        # BULK: one load + one save for the whole batch (O(N), not N whole-file
        # rewrites) — this is the per-run cost, so it must not be O(N²).
        return FactStore(vault).put_facts(facts)
    except Exception:
        logger.debug("[world-model] populate_from_situation skipped (non-fatal)", exc_info=True)
        return 0
