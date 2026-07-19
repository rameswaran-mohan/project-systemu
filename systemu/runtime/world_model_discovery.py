"""R-W1 (W-A slice-2c) — the discovery negative-fact loop (WM-2 · §5.5 · IMPL-14).

"Searched and did NOT find" becomes a persisted, EXPIRING fact so a futile search is
never silently forgotten (today it is) and a handoff can cite what was probed and when
(§5.11 AC2) instead of stalling. This module owns the WRITE side of that loop at the
discovery-before-forge seam (``discovery_pass``, Seam A in ``shadow_runtime``).

Three rules shape it, each learned from an earlier slice:

**1. The origin is HARD-CODED, not a parameter.** Every negative written here is stamped
``systemu_authored``, and there is deliberately NO argument through which a caller could
supply an origin. Absence that untrusted content can assert is denial-of-discovery — a
poisoned artifact claiming "there is nothing here" would suppress a real search for the
whole TTL. ``NegativeFact`` also refuses ``content_derived`` at construction, so this is
belt-and-braces: the writer cannot express it and the model would reject it.

**2. Absence is only recorded when we ACTUALLY LOOKED.** ``discovery_pass`` returns
``searched=0`` for an empty or unreadable catalog — that is "we surveyed nothing", not
"nothing exists". Recording a negative there would infer absence from our own empty
output, which is exactly the bug the read-side staleness work fixed (~20 present-on-disk
files reported as "may be gone" because truncation was invisible). Coverage comes from
the searcher's own report of what it ranked; with no coverage we write nothing.

**3. A negative is INVALIDATED ON WRITE, not only by TTL.** When discovery later resolves
the same name to a real tool, the "searched, not found" note is dropped immediately
(CAP-5). A stale suppression that outlives the thing it described is the failure mode
that makes negative caching untrustworthy.

SCOPE: this module WRITES the store and is read only by operator-facing surfaces. It does
NOT feed the planner — nothing here is added to ``_req.spec`` or any prompt, so planner
input is byte-identical to the previous slice. In particular the deterministic local pass
is still ALWAYS run: skipping it on a negative-fact hit is the one thing this module
deliberately does not do (see :func:`recent_discovery_miss`).
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from systemu.runtime.world_model import FactStore, NegativeFact

logger = logging.getLogger(__name__)

_NORM = re.compile(r"[^a-z0-9]+")

#: A capability-discovery miss expires FAST. The generic WM-2 default (6h) is tuned for
#: an expensive external search; the local catalog can change the moment a forge lands,
#: so a shorter horizon keeps a note from outliving the world it described. Invalidation
#: on write (:func:`clear_discovery_miss`) is the primary mechanism — this TTL is the
#: backstop for the paths that never call it.
DISCOVERY_MISS_TTL_SECONDS = 60 * 60          # 1 hour

#: Bound on what one note records, so a pathological name can't grow the store.
_MAX_PROBES = 8


def scope_for(requested_name: str) -> str:
    """The canonical negative-fact scope key for a capability search.

    Normalised (lowercase, non-alphanumerics collapsed) so ``Send_Invoice`` and
    ``send invoice`` share one note rather than each re-paying the search. Returns ``""``
    for a nameless request — the caller MUST treat that as "do not record", since a note
    keyed on the empty string would suppress every future unnamed search at once."""
    norm = _NORM.sub("_", str(requested_name or "").strip().lower()).strip("_")
    return f"capability:{norm}" if norm else ""


def _probes_for(result: Any) -> List[str]:
    """What the pass actually probed — non-secret descriptors only (E6), so a handoff can
    say WHAT was searched. Never raises.

    The score is recorded as a plain OBSERVATION, never as a claim about why the search
    failed. ``discovery_pass`` only reuses on an EXACT normalized-name match, so a miss
    routinely carries a score ABOVE the floor (a strong fuzzy match under a different
    name). An earlier draft rendered this as ``best_score=25.0<floor=8.0`` — literally
    false, and false in the citation is worse than absent, because the whole point of a
    negative fact is that a handoff can trust what it says."""
    try:
        searched = int(getattr(result, "searched", 0) or 0)
        best = float(getattr(result, "best_score", 0.0) or 0.0)
        floor = float(getattr(result, "floor", 0.0) or 0.0)
    except Exception:
        return ["deployed_enabled_vault_catalog"]
    return [
        f"deployed_enabled_vault_catalog(n={searched})",
        f"no exact name match (best_score={best:.1f}, reuse_floor={floor:.1f})",
    ][:_MAX_PROBES]


def record_discovery_miss(vault: Any, requested_name: str, result: Any) -> Optional[NegativeFact]:
    """Persist "searched for ``requested_name`` and found nothing" (WM-2).

    Returns the stored :class:`NegativeFact`, or ``None`` when nothing was recorded —
    which happens, deliberately, in three cases:

      * the request had no usable name (see :func:`scope_for`);
      * ``result`` reports a HIT — a hit is not a miss;
      * ``result.searched <= 0`` — we ranked NOTHING, so we have no coverage and absence
        is not evidence (rule 2 in the module docstring).

    WRITE-ONLY and FAIL-SAFE: it never raises, so a store problem can never break the
    forge path it hangs off. The ``systemu_authored`` stamp is hard-coded (rule 1)."""
    try:
        if result is None or getattr(result, "reuse_tool_id", None):
            return None
        scope = scope_for(requested_name)
        if not scope:
            return None
        if int(getattr(result, "searched", 0) or 0) <= 0:
            # We surveyed nothing. "I looked at zero tools" is not evidence that the tool
            # does not exist — recording it would manufacture absence from our own empty
            # output and then suppress the real search for the whole TTL.
            return None
        neg = NegativeFact(
            scope=scope,
            probes=_probes_for(result),
            ttl_seconds=DISCOVERY_MISS_TTL_SECONDS,
            # HARD-CODED: systemu's own surveyor asserted this absence. There is no
            # parameter here on purpose — see rule 1.
            origin_class="systemu_authored",
        )
        FactStore(vault).put_negative(neg)
        return neg
    except Exception:
        logger.debug("[world-model] discovery-miss note skipped (non-fatal)", exc_info=True)
        return None


def clear_discovery_miss(vault: Any, requested_name: str) -> bool:
    """Invalidate the note for ``requested_name`` because the capability now EXISTS
    (rule 3 / CAP-5). Returns True if a note was dropped. Never raises."""
    try:
        scope = scope_for(requested_name)
        if not scope:
            return False
        return bool(FactStore(vault).drop_negative(scope))
    except Exception:
        logger.debug("[world-model] discovery-miss clear skipped (non-fatal)", exc_info=True)
        return False


def recent_discovery_miss(vault: Any, requested_name: str,
                          now: Optional[str] = None) -> Optional[NegativeFact]:
    """The unexpired note for ``requested_name``, or ``None``.

    READ-ONLY, and deliberately **not** wired as a search-skip at the ``discovery_pass``
    seam. WM-2's "skip a known-absent path within TTL" targets EXPENSIVE discovery (a
    budgeted registry/docs/introspection ladder, IMPL-14). The pass this slice hangs off
    is a pure, deterministic, in-memory ranking over an already-loaded catalog: skipping
    it would save nothing measurable while adding a real failure mode — a note that
    outlives its world suppressing a reuse that is now available. So the value here is
    the CITATION (AC2's "the handoff cites what was searched when"), consumed by the
    operator surfaces. The skip belongs with the budgeted ladder when it lands.

    Never raises."""
    try:
        scope = scope_for(requested_name)
        if not scope:
            return None
        return FactStore(vault).query_negative(scope, now=now)
    except Exception:
        logger.debug("[world-model] discovery-miss read skipped (non-fatal)", exc_info=True)
        return None
