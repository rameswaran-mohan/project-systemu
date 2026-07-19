"""R-W1 (W-A slice-2c) — ``world.query`` as the FIRST *fenced* READ surface (WM-4/WM-15).

Slices 1/2a/2b built the store and a WRITE-ONLY populator; a test pins that only an
allowlist of modules may even reference the world model, which is what has kept the
agent's behaviour identical whether the store is absent, empty, or full. This module is
the first deliberate widening of that allowlist on the READ side.

It exposes the §5.11 WM-4 view family (``find_services`` / ``what_can`` / ``find_data`` /
``about`` / ``provenance``, all defined in :mod:`world_model`) as a surface whose results
are **fenced untrusted data** (WM-15), rendered through the SAME BLOCKER-2 fence the
SituationReport already uses. Two properties are load-bearing and separately pinned:

**1. Bind-taint is RE-DERIVED, never read from the store.** A stored ``Fact`` carries an
honest *provenance* ``origin_class`` (who asserted it) — but the populator copies each
inventory entry's DECLARED origin, and the service model defaults every service to
``operator``. Trusting that field at a bind would let a surveyed value flip ask→silent:
a straight IMPL-5 regression. :func:`bind_taint_of` therefore mirrors
``requirement_binder._entry_origin`` EXACTLY — an unconditional ``content_derived``. A
store read can never, by construction, produce a taint that permits a silent bind.

**2. The prompt-facing row OMITS the launderable fields.** ``origin_class`` and
``confidence`` are not merely down-ranked — they are absent from the fenced payload.
The populator stamps ``confidence=1.0`` on every fact, so emitting it would assert
certainty the store does not have, and emitting ``origin_class`` would re-offer the very
field property 1 exists to neuter. What IS emitted in their place is ``staleness``, which
is derived read-side from the survey WATERMARK (what the surveyor says it covered) rather
than inferred from our own output — absence is not evidence (§5.11 read-side staleness).
The operator CLI still shows the stored fields: it is off every decision path.

SCOPE (slice-2c): this module is a read API + its fence. It is NOT injected into the
planner prompt — the "SituationReport becomes a ranked view over the store" inversion is
the LAST step of W-A and is deliberately deferred, so this slice leaves planner input
byte-identical and is verifiable without changing what the model sees. A test pins that
the planner-input builder does not reference this module.
"""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

from systemu.runtime import world_model as _wm
from systemu.runtime.situational_inventory import fence

logger = logging.getLogger(__name__)

_CONTENT_DERIVED = "content_derived"

#: The fields a fenced row is ALLOWED to carry. An allowlist, not a blocklist: a field
#: added to ``Fact`` later cannot leak into a prompt by default (fail-closed).
FENCED_ROW_FIELDS = ("fact_id", "kind", "value", "bind_taint", "staleness")

#: Explicitly NEVER rendered into a prompt-facing row (see property 2 in the module
#: docstring). Pinned by test, so deleting a field from the builder is not enough to
#: silently re-introduce one.
NEVER_FENCED_FIELDS = ("origin_class", "confidence")


def bind_taint_of(fact: Any) -> str:
    """The bind-taint for a fact READ FROM THE STORE — always ``content_derived``.

    This deliberately IGNORES ``fact.origin_class``, mirroring
    ``requirement_binder._entry_origin`` (whose body is likewise a single unconditional
    return). The reasoning is identical: the stored stamp is a plain unvalidated string
    copied from a surveyed entry, so a poisoned or merely default-y report could carry
    ``operator`` and launder an untrusted value into the trusted axis. Every fact in the
    store today arrives via the inventory populator, i.e. from scanned content.

    If a genuinely operator-authored write path is added later, it must clear taint at
    ITS OWN site with a hard-coded origin (as ``_bind_profile`` does) — never by teaching
    this function to trust a stored field. Pure; never raises."""
    return _CONTENT_DERIVED


def fenced_row(fact: Any, survey: Optional[Any] = None) -> dict:
    """One prompt-safe row: identity + value + a RE-DERIVED taint + honest staleness.

    Never raises — a malformed fact degrades to a row with an empty value rather than
    breaking a whole view."""
    try:
        staleness = _wm.staleness_of(fact, survey)
    except Exception:
        staleness = "unknown"
    return {
        "fact_id": str(getattr(fact, "fact_id", "") or ""),
        "kind": str(getattr(fact, "kind", "") or ""),
        "value": getattr(fact, "value", None),
        # RE-DERIVED (property 1) — NOT fact.origin_class.
        "bind_taint": bind_taint_of(fact),
        # From the SURVEYOR's coverage, not from our own output (absence is not evidence).
        "staleness": staleness,
    }


def render_facts_for_prompt(facts: List[Any], *, query: str = "",
                            survey: Optional[Any] = None) -> str:
    """Render world.query results as a FENCED, deterministic JSON block (WM-15).

    Results describe WHAT EXISTS; they are never instructions. Uses the same
    ``situational_inventory.fence`` (nonce'd, delimiter-neutralising, fail-closed) that
    already wraps the SituationReport, so there is ONE fence implementation to audit."""
    try:
        body = json.dumps(
            {"query": str(query or ""),
             "results": [fenced_row(f, survey) for f in (facts or [])]},
            sort_keys=True, default=str)
    except Exception:
        body = json.dumps({"query": str(query or ""), "results": []}, sort_keys=True)
    return fence(body)


def render_negative_for_prompt(neg: Any) -> str:
    """Render a WM-2 negative fact ("searched and did NOT find") as fenced data.

    Carries what was probed and WHEN (AC2's citation half) so a handoff can be precise
    instead of a silent stall. Fenced like any other world-model read: a negative fact is
    still a description of the world, not an instruction."""
    if neg is None:
        return fence(json.dumps({"searched_and_not_found": None}, sort_keys=True))
    try:
        body = json.dumps({"searched_and_not_found": {
            "scope": str(getattr(neg, "scope", "") or ""),
            "probes": [str(p) for p in (getattr(neg, "probes", None) or [])],
            "recorded_at": str(getattr(neg, "recorded_at", "") or ""),
            "ttl_seconds": int(getattr(neg, "ttl_seconds", 0) or 0),
        }}, sort_keys=True, default=str)
    except Exception:
        body = json.dumps({"searched_and_not_found": None}, sort_keys=True)
    return fence(body)
