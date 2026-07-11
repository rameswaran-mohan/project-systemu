"""R-A13b-2iii — the SHADOW park-surface REPORT reader helpers + the Stage-3 ARM-GATE.

This module closes the measure→decide loop for the external-verification net (R-A13,
the 3-stage OFF/SHADOW/ENFORCE activation). The v0.9.71 SHADOW meter already RECORDS a
per-effect-class bucket ``s4_shadow = {effect_class: {would_stamp, would_credit,
would_park}}`` (``metrics_store.incr_s4_shadow_meter`` at the credit seam;
``shadow_meter_snapshot()`` reads it back). Until now that snapshot was read only by
tests — nobody surfaced the park surface to a human or a decision.

This module is **pure read-side plumbing**: it changes NO credit/meter behaviour and
writes nothing. Two things live here:

* ``s4_shadow_arm_verdict`` — the pure Stage-3 arm-gate: given the snapshot, decide
  whether the net is ready to flip ENFORCE, with a criterion that does NOT reduce to
  ``would_park==0`` (that is wrong twice — it never passes on genuinely-unverifiable
  effects, and it falsely passes when nothing ever stamped).
* ``format_shadow_meter_rows`` / ``arm_verdict_line`` — pure formatting helpers the CLI
  report reader (``debug s4-shadow-meter``) renders, kept out of the CLI so they are
  unit-testable without a console.

Effect-class key strings match what the meter writes: normalized-lower ``EffectTag.value``
strings plus ``"unknown"`` (the DEC-24 UNKNOWN-until-classified bucket).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# The DEC-24 stamp set fallback (kept in sync with requirement_binder._stamp_effect_values;
# imported lazily below so the single source of truth is that function, but the arm-gate
# still never raises even if that import ever fails).
_STAMP_SET_FALLBACK = frozenset({"net_mutate", "money_move", "send_message", "oauth_call"})

# "unknown" is stamped-until-classified (DEC-24 / BLOCKER-3): it stamps like a positive
# effect until effect_tags resolve it, so for the arm-gate it is a STAMPED class (subject
# to the live-channel check), NOT a benign-over-stamp violation.
_UNKNOWN = "unknown"


def _stamp_set() -> frozenset:
    """The DEC-24 positive stamp set (single source of truth). Never raises."""
    try:
        from systemu.runtime.requirement_binder import _stamp_effect_values
        s = _stamp_effect_values()
        return frozenset(str(v).strip().lower() for v in s) if s else _STAMP_SET_FALLBACK
    except Exception:
        return _STAMP_SET_FALLBACK


def _int(v: Any) -> int:
    """Defensive int coercion — a None/blank/garbage value reads as 0. Never raises."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _cell(snapshot: Dict[str, Any], effect_class: str) -> Dict[str, int]:
    """Read one effect-class cell defensively as ``{would_stamp, would_credit, would_park}``."""
    raw = snapshot.get(effect_class) if isinstance(snapshot, dict) else None
    if not isinstance(raw, dict):
        raw = {}
    return {
        "would_stamp": _int(raw.get("would_stamp")),
        "would_credit": _int(raw.get("would_credit")),
        "would_park": _int(raw.get("would_park")),
    }


def s4_shadow_arm_verdict(snapshot: Any, *, min_runs: int) -> Tuple[bool, List[str]]:
    """The pure Stage-3 arm-gate verdict over the ``s4_shadow`` park-surface snapshot.

    ``snapshot`` = ``{effect_class: {would_stamp, would_credit, would_park}}`` (as returned
    by ``MetricsStore.shadow_meter_snapshot()``). Returns ``(ready, reasons)`` where
    ``reasons`` is the list of FAILING checks (empty ⇔ ready). PURE (no I/O); never raises;
    defensive on malformed cells.

    Criterion (deliberately NOT ``would_park==0``):

      1. COVERAGE — Σ would_stamp across all classes >= ``min_runs`` (enough evidence to
         trust the surface). Else ``"insufficient data (N/min_runs)"``.
      2. NO BENIGN OVER-STAMP — every class OUTSIDE the DEC-24 stamp set AND not ``"unknown"``
         must have would_stamp == 0. A benign class (local_read/net_read/…) reaching the
         stamp meter proves effect_tags aren't populated ⇒ its parks are SPURIOUS. Else
         ``"benign class <c> still stamping (N)"``.
      3. LIVE CHANNEL — every STAMPED class (stamp set ∪ {"unknown"}) with would_stamp>0 must
         have would_credit>0. A stamped class that only ever parks ⇒ dead evidence channel ⇒
         its parks are SPURIOUS. Else ``"stamped class <c> has 0 would_credit (dead channel)"``.

    Reasons are appended deterministically: coverage, then benign-over-stamp (sorted by
    class), then dead-channel (sorted by class).
    """
    reasons: List[str] = []
    if not isinstance(snapshot, dict):
        snapshot = {}
    stamp_set = _stamp_set()

    classes = [str(k) for k in snapshot.keys()]

    # 1. COVERAGE
    total_stamp = sum(_cell(snapshot, c)["would_stamp"] for c in classes)
    try:
        mr = int(min_runs)
    except (TypeError, ValueError):
        mr = 0
    if total_stamp < mr:
        reasons.append(f"insufficient data ({total_stamp}/{mr})")

    # 2. NO BENIGN OVER-STAMP (a class outside the stamp set AND not the unknown bucket)
    for c in sorted(classes):
        if c in stamp_set or c == _UNKNOWN:
            continue
        n = _cell(snapshot, c)["would_stamp"]
        if n > 0:
            reasons.append(f"benign class {c} still stamping ({n})")

    # 3. LIVE CHANNEL (every stamped class that stamped must have credited at least once)
    for c in sorted(classes):
        if not (c in stamp_set or c == _UNKNOWN):
            continue
        cell = _cell(snapshot, c)
        if cell["would_stamp"] > 0 and cell["would_credit"] == 0:
            reasons.append(f"stamped class {c} has 0 would_credit (dead channel)")

    return (not reasons), reasons


def _park_rate(stamp: int, park: int) -> float:
    """would_park / would_stamp, defensive on a zero-stamp cell (⇒ 0.0)."""
    return (park / stamp) if stamp > 0 else 0.0


def format_shadow_meter_rows(snapshot: Any) -> List[Dict[str, Any]]:
    """Pure per-effect-class rows for the CLI table, sorted by effect_class.

    Each row: ``{effect_class, would_stamp, would_credit, would_park, park_rate}``.
    Defensive — a malformed snapshot/cell yields zeros, never raises.
    """
    if not isinstance(snapshot, dict):
        return []
    rows: List[Dict[str, Any]] = []
    for c in sorted(str(k) for k in snapshot.keys()):
        cell = _cell(snapshot, c)
        rows.append({
            "effect_class": c,
            "would_stamp": cell["would_stamp"],
            "would_credit": cell["would_credit"],
            "would_park": cell["would_park"],
            "park_rate": _park_rate(cell["would_stamp"], cell["would_park"]),
        })
    return rows


def arm_verdict_line(snapshot: Any, *, min_runs: int) -> str:
    """A one-line human summary of the arm-gate verdict for the CLI footer."""
    ready, reasons = s4_shadow_arm_verdict(snapshot, min_runs=min_runs)
    if ready:
        return f"ARM VERDICT: READY (coverage >= {min_runs}, no spurious parks)"
    return "ARM VERDICT: NOT_READY — " + "; ".join(reasons)
