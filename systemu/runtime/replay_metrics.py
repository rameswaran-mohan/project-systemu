"""R-A13.5 — deterministic replay metrics over the accreted corpus (§10 / CAP-10).

The measurement substrate the DEC-7 (ask-cap) decision, R-A16/G-LEARN, and CAP-10
consume. DETERMINISTIC post-hoc replay only — never an LLM judge (IMPL-15 discipline)
— so the numbers are replay-stable and defensible.

Slice-1 (here): the **avoidable-forge** metric (CAP-10). For every forged tool, the
capability-slot query is re-run with hindsight: does an EXISTING tool already occupy
that tool's slot (i.e. would it have bound instead of forging a duplicate)? The rate
is the CAP-10 tripwire that adjudicates the DEC-18 "no embeddings" (CAP-8) question,
reported beside the §10 avoidable-ask rate. Computable over the live vault today
(reuses the shipped R-CAP1 index — `capability_index.slot_collisions`).

The **avoidable-ask** metric (§10, decides DEC-7) is slice-2: it needs the recorded
ask corpus (a `ReplayScenario` per run's missing-required ask + its inventory state);
its fixture format is the plan's "FIRST STEP" and lands with the ask-replay.
"""
from __future__ import annotations

from typing import Any, Dict, List


def avoidable_forge_report(vault) -> Dict[str, Any]:
    """CAP-10 — the avoidable-forge rate over the vault's forged tools.

    A forged tool is AVOIDABLE if an existing tool WOULD HAVE BOUND instead of
    forging it. Faithful to "instead of forging" (not a symmetric slot-duplicate
    count — the adversarial-review fix): for a forged tool F occupying slot S,

      • a NON-forged (builtin/MCP) tool in S ⇒ avoidable (it would have bound); else
      • among forged-only occupants of S, the FIRST forge into the (then-empty) slot
        was NOT avoidable — so exactly k-1 of k forged-only occupants count (the
        deterministic 'first' = the min tool_id; the rest are the redundant extras).

    Forged tools with no derivable slot are UNASSESSABLE (reported separately, kept
    out of numerator AND denominator so they don't deflate the rate). Deterministic,
    read-only (live in-memory index derive, never writes), never raises."""
    try:
        from systemu.runtime import capability_index as ci
        from systemu.runtime import capability_slots as cs
    except Exception:
        return _empty()
    try:
        rows = vault.list_tools() or []
    except Exception:
        return _empty()
    forged = [t for t in rows if isinstance(t, dict) and t.get("forged_by_systemu")]

    def _primary_slot(name: str) -> str:
        s = cs.slots_from_name(name or "")
        return cs.slot_str(s[0]) if s else ""

    # occupancy from the live index: which slots hold a NON-forged (builtin/MCP)
    # tool, and the names of those would-be binders per slot.
    try:
        index = list(ci.derive_index(vault) or [])
    except Exception:
        index = []
    slot_nonforged_names: Dict[str, set] = {}
    for r in index:
        origin = str(getattr(r, "origin", "") or "")
        if origin.startswith("forged"):
            continue
        nm = str(getattr(r, "name", "") or "")
        for s in (getattr(r, "slots", []) or []):
            slot_nonforged_names.setdefault(s, set()).add(nm)

    # forged tools grouped by their primary slot (for the k-1 first-forge rule)
    forged_slot: Dict[str, str] = {}
    slot_forged_ids: Dict[str, List[str]] = {}
    for t in forged:
        tid = str(t.get("id", "") or "")
        s = _primary_slot(str(t.get("name", "") or ""))
        forged_slot[tid] = s
        if s:
            slot_forged_ids.setdefault(s, []).append(tid)

    avoidable: List[Dict[str, Any]] = []
    unassessable = 0
    for t in forged:
        tid = str(t.get("id", "") or "")
        name = str(t.get("name", "") or "")
        s = forged_slot.get(tid, "")
        if not s:
            unassessable += 1
            continue
        binders = slot_nonforged_names.get(s)
        if binders:
            avoidable.append({"tool_id": tid, "name": name, "slots": [s],
                              "would_bind": sorted(x for x in binders if x)})
            continue
        siblings = slot_forged_ids.get(s, [])
        if len(siblings) >= 2 and tid != min(siblings):
            first = min(siblings)
            fb = sorted({str(x.get("name", "")) for x in forged
                         if str(x.get("id", "")) == first and x.get("name")})
            avoidable.append({"tool_id": tid, "name": name, "slots": [s],
                              "would_bind": fb})

    assessable = len(forged) - unassessable
    return {
        "total_forged": len(forged),
        "assessable": assessable,
        "unassessable_no_slot": unassessable,
        "avoidable_count": len(avoidable),
        "rate": (len(avoidable) / assessable) if assessable else 0.0,
        "avoidable": avoidable,
    }


def _empty() -> Dict[str, Any]:
    return {"total_forged": 0, "assessable": 0, "unassessable_no_slot": 0,
            "avoidable_count": 0, "rate": 0.0, "avoidable": []}


def format_avoidable_forge(report: Dict[str, Any]) -> List[str]:
    """Plain-string report lines (for a CLI / debug surface)."""
    r = report or {}
    assessable = int(r.get("assessable", r.get("total_forged", 0)) or 0)
    n = int(r.get("avoidable_count", 0) or 0)
    rate = float(r.get("rate", 0.0) or 0.0)
    unassessable = int(r.get("unassessable_no_slot", 0) or 0)
    lines = [
        f"Avoidable-forge rate: {n}/{assessable} = {rate * 100:.0f}%",
        "  (a forged tool an EXISTING tool would have bound instead of forging — CAP-10;",
        "   deterministic replay, never an LLM judge)",
    ]
    if unassessable:
        lines.append(f"  ({unassessable} forged tool(s) have no derivable slot — "
                     f"unassessable, excluded from the rate)")
    for it in (r.get("avoidable") or []):
        wb = ", ".join(it.get("would_bind") or []) or "?"
        slot = ", ".join(it.get("slots") or []) or "-"
        lines.append(f"  · {it.get('name', '')} [{slot}] — would have bound: {wb}")
    if not (r.get("avoidable")):
        lines.append("  · none — no forged tool duplicates an existing slot.")
    return lines
