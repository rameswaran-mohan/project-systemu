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

import json
from pathlib import Path
from typing import Any, Dict, List


# ── avoidable-ASK corpus (§10, decides DEC-7) — slice-2 ────────────────────────
# A deterministic directional signal accreted from real runs. The ask rail records
# each harness ask with its resolution-attempt instrumentation (attempts_before,
# tool_attempts, blocked_signals — the v0.10.0 pull instrumentation); the report
# counts asks made with NO recorded resolution attempt — a §10 lower-bound. The
# corpus is append-only, single-writer (the shadow exec thread) — CONC-MAP registered.

def _ask_corpus_path(vault) -> Path:
    return Path(vault.root) / "audit" / "ask_corpus.jsonl"


def record_ask(vault, *, kind: str = "", attempts_before: int = 0,
               blocked_signals: Any = None, tool_attempts: int = 0,
               confidence: float = 0.5) -> None:
    """Append one ask to the corpus (R-A13.5 / DEC-11 accretion). OBSERVABILITY-ONLY,
    append-only, single-writer — NEVER raises (a recording hiccup must never affect
    the run that made the ask)."""
    try:
        rec = {
            "kind": str(kind or ""),
            "attempts_before": int(attempts_before or 0),
            "tool_attempts": int(tool_attempts or 0),
            "blocked_signals": list(blocked_signals or []),
            "confidence": float(confidence or 0.0),
        }
        p = _ask_corpus_path(vault)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def load_ask_corpus(vault) -> List[Dict[str, Any]]:
    """All recorded asks. Defensive: a broken/absent file / malformed line ⇒ skipped."""
    try:
        p = _ask_corpus_path(vault)
        if not p.exists():
            return []
        out: List[Dict[str, Any]] = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out
    except Exception:
        return []


def avoidable_ask_report(vault) -> Dict[str, Any]:
    """§10 — a DETERMINISTIC DIRECTIONAL signal for the avoidable-ask rate (a DEC-7
    input). Counts asks made with NO recorded resolution attempt (zero tool-attempts
    AND no blocking signal): by §10 ("try inventory + discovery + resolver + a safe
    default BEFORE asking") those are avoidable-CANDIDATES. This is a NON-DEFINITIVE
    PROXY / leading indicator, not a strict bound — its bias is two-sided (it can miss
    an avoidable ask that logged a *failed* tool attempt, and can count a necessary
    ACCESS/COMPUTE ask that legitimately did no tool attempt). The definitive rate
    needs a resolver-replay over each ask's inventory snapshot (the documented
    refinement). Operator-input (``kind=="input"``) asks are excluded — necessary by
    nature. Reported beside the avoidable-forge rate. Never raises."""
    corpus = [a for a in load_ask_corpus(vault)
              if str(a.get("kind", "")).strip().lower() != "input"]
    total = len(corpus)
    no_attempt = [
        a for a in corpus
        if int(a.get("attempts_before") or 0) == 0
        and int(a.get("tool_attempts") or 0) == 0
        and not (a.get("blocked_signals") or [])
    ]
    return {
        "total_asks": total,
        "no_attempt_count": len(no_attempt),
        "rate": (len(no_attempt) / total) if total else 0.0,
    }


def format_avoidable_ask(report: Dict[str, Any]) -> List[str]:
    r = report or {}
    total = int(r.get("total_asks", 0) or 0)
    n = int(r.get("no_attempt_count", 0) or 0)
    rate = float(r.get("rate", 0.0) or 0.0)
    return [
        f"No-prior-attempt asks: {n}/{total} = {rate * 100:.0f}%",
        "  (asks made with no recorded tool-resolution attempt and no blocking signal —",
        "   a deterministic DIRECTIONAL signal (non-definitive proxy) for the §10",
        "   avoidable-ask rate; a DEC-7 input. The definitive rate needs a resolver-replay",
        "   over each ask's inventory snapshot.)",
    ]


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
