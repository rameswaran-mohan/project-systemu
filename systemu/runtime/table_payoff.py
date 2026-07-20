"""R-B5 / T5 — the OnTheTable PAYOFF surfaces (spec §5.10.c chips, §5.10.d, §10).

Three read-only consumers of the attribution R-B5 added to the bind
(``Requirement.table_item_id``):

  * :func:`using_from_table` — the during-task *"using from your table: X · Y"*
    chip row (§5.10.c).
  * :func:`answered_from_table` — the completion chip, **≤1 per task and
    novelty-gated** (§5.10.c). The RAW count still feeds the metric when the chip
    is suppressed — suppression is a UI rule, never a measurement rule.
  * :func:`inventory_hit_report` — the §10 inventory-hit metric.

Everything here is OBSERVABILITY. Nothing in this module may change a bind, a
taint, a confidence, or an ask decision; the only write is the novelty ledger.


THE ONE THING TO UNDERSTAND BEFORE EDITING THIS FILE
────────────────────────────────────────────────────
**A table-backed bind can NEVER be silent, so "answered from your table" can never
mean "bound with no operator interaction."**

``requirement_binder._entry_origin`` clamps EVERY surveyed inventory entry to
``content_derived`` — unconditionally, by design (IMPL-5 fail-untrusted: a survey
entry's ``origin_class`` is an unvalidated string and a poisoned/rehydrated report
could forge ``operator``). ``_needs_ask`` then puts every ``content_derived`` bind
in the ask_bundle regardless of confidence. Composing the two: a curated,
operator-added, perfectly-matched table item still lands a one-click confirm.

So a chip defined as *"table-supplied AND not in the ask_bundle"* would be
**structurally always zero** — not rarely zero, never non-zero — and would read as
a working feature that simply had nothing to celebrate. (This is the same failure
shape as ``IndexRow.effect_tags``, which no producer populates, so every consumer
silently no-ops.) ``test_a_table_bind_is_never_silent`` pins the clamp so that if
anyone ever relaxes it, the pin fails and this comment gets revisited.

**What the table actually buys, and therefore what these surfaces count:** it turns
a ``missing`` gap — a leaf no source could fill, which the operator must supply
from nothing — into a PRE-FILLED one-click confirm. That is a real, honest,
measurable payoff, and it needs no taint relaxed to be true. The metric therefore
reports ``silent`` and ``prefilled_confirm`` as SEPARATE numerator components and
never adds them into a single headline number that would hide which one moved.

**Spec tension, RESOLVED by DEC-25 (§5.10.e AC7).** AC7 used to read "a requirement
resolvable from a table item binds without an ask", which under the IMPL-5 clamp is
unsatisfiable for every table-backed source. DEC-25 ruled that wording a drafting
artifact: it conflated eliminating the *elicitation* (the from-scratch ``missing``
ask, which the table genuinely removes) with eliminating the *confirmation* (the
security checkpoint, which it must not). The clamp stands untouched.

AC7 now reads: **table-sourced ``silent`` is STRUCTURALLY ZERO, and a nonzero
reading is a CLAMP REGRESSION, not payoff.** That inverted reading is what
:func:`clamp_tripwire` implements — ``table_silent`` is a healthy-is-zero alarm
wired into the §10 surface and into the live producer, so that if anyone ever
punches ``_entry_origin`` or ``_needs_ask`` the number becomes VISIBLE rather than
being reported as improved payoff. Do NOT fold a future "endorsed" bind (an
operator having explicitly vouched for an inventory entry) into ``silent``: DEC-25
forbids it precisely because it would re-populate this counter with benign traffic
and kill the alarm. An endorsed bind gets its own THIRD counter.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

# THE ask predicate — imported, never re-implemented (see the note on _needs_ask
# below). Module-level and by-name on purpose: a lazy/guarded import could fall back
# to a local copy on failure, which is the exact drift this import exists to prevent.
# requirement_binder imports no systemu module at import time, so there is no cycle.
from systemu.runtime.requirement_binder import _needs_ask

logger = logging.getLogger(__name__)

#: Max items named in the during-task chip row (§5.10.c "X · Y" is a strip, not a
#: list). Extra items are counted in ``overflow``, never dropped silently.
MAX_USING_CHIPS = 4

#: A bind stops being celebrated once the same table item has been celebrated this
#: many times (§5.10.c novelty gate). The COUNT still feeds §10.
NOVELTY_CELEBRATION_LIMIT = 3

#: Ceiling on the novelty ledger so a long-lived vault cannot grow it without bound.
MAX_NOVELTY_KEYS = 200


# ── reading the requirement report ──────────────────────────────────────────
def _get(obj: Any, field: str) -> Any:
    """Read a field off a pydantic Requirement OR a plain dict.

    Both shapes are live: the binder returns models, but a report that has
    round-tripped through the ExecutionSnapshot comes back as dicts. A
    getattr-only read would work in every unit test and return nothing in
    production on the resumed path.
    """
    if isinstance(obj, dict):
        return obj.get(field)
    return getattr(obj, field, None)


def _requirements(report: Any) -> List[Any]:
    """Flatten a RequirementReport (or a bare list) into one requirement list.

    Deliberately reads ``per_objective`` and NOT ``ask_bundle``: the bundle is a
    deduped SUBSET, so counting it would silently drop every silent bind — i.e.
    exactly the numerator the metric exists to measure. Never raises.
    """
    try:
        if report is None:
            return []
        if isinstance(report, list):
            return [r for r in report if r is not None]
        per = _get(report, "per_objective")
        if isinstance(per, dict):
            out: List[Any] = []
            for rows in per.values():
                if isinstance(rows, list):
                    out.extend(r for r in rows if r is not None)
            return out
        return []
    except Exception:
        logger.debug("[table_payoff] requirement flatten failed", exc_info=True)
        return []


# ── the ask predicate: ONE implementation, not two ──────────────────────────
# This module used to carry ``_is_ask``, a HAND-MIRROR of the binder's ``_needs_ask``,
# justified by "that helper takes the binder's own ``_get`` and this module must also
# read snapshot dicts". That justification was FALSE: ``requirement_binder._get`` is
# behaviourally identical to this module's ``_get`` — it already does the
# ``isinstance(obj, dict)`` branch — so the binder's predicate reads a rehydrated
# snapshot dict perfectly well. The mirror bought nothing and cost a drift surface.
#
# Why that mattered enough to DELETE rather than pin: the DEC-25 tripwire below RESTS
# on this predicate. A tripwire resting on a mirror is worthless the moment the mirror
# drifts from the original — it would keep reading a healthy zero while the real binder
# had changed underneath it. The old ``test_is_ask_tracks_the_binder`` "protected"
# against that by asserting the two paths AGREE, which is the weak form: two paths that
# agree today can be changed together and the pin passes either way. Collapsing to a
# single projection makes divergence STRUCTURALLY impossible instead of merely watched.
#
# ``_needs_ask`` is therefore imported at module scope (top of file) and used directly.
# Two pins hold that shut, and they are deliberately DIFFERENT assertions:
#   * an IDENTITY pin (``table_payoff._needs_ask is requirement_binder._needs_ask``)
#     — nothing may hide a reimplementation behind the name; and
#   * a CALL-SITE pin (monkeypatch this module's name, watch the counter move)
#     — nothing may keep the name around while routing the count past it.


def table_backed(report: Any) -> List[Any]:
    """Every requirement the operator's table supplied (carries a table_item_id)."""
    return [r for r in _requirements(report)
            if isinstance(_get(r, "table_item_id"), str) and _get(r, "table_item_id")]


# ── §10 inventory-hit metric ────────────────────────────────────────────────
def inventory_hit_report(report: Any) -> Dict[str, Any]:
    """The §10 inventory-hit metric over one run's RequirementReport.

    ``supplied`` — requirements the SituationReport bound (``source == "situation"``).
    ``avoided_gap`` — of those, the ones that did NOT end as a ``missing`` gap: the
    honest payoff, since the report's contribution is turning "you must supply this
    from nothing" into "confirm this".
    ``silent`` / ``prefilled_confirm`` — ``avoided_gap`` split by whether the
    operator was asked at all. Kept SEPARATE on purpose: under the IMPL-5 clamp
    ``silent`` is reachable only via the credential-NAME branch, so summing them
    into one headline would let a collapse in ``silent`` hide behind confirms.
    ``table_*`` — the same counts restricted to table-attributed binds.

    ``table_silent`` — **the DEC-25 tripwire, and the one counter here that is NOT a
    payoff number.** Healthy is ZERO, structurally: ``_entry_origin`` clamps every
    surveyed entry to ``content_derived`` and ``_needs_ask`` always asks for
    ``content_derived``, so a table-attributed bind cannot go silent while the clamp
    holds. A nonzero reading therefore means the clamp was punched, not that the
    table got better. Read it with :func:`clamp_tripwire`, which states that polarity
    in its return value so a caller cannot mistake the alarm for a win.

    ``rate`` is ``avoided_gap / supplied``, and is ``0.0`` (not a division error,
    not None) when nothing was supplied. Never raises.
    """
    try:
        reqs = _requirements(report)
        supplied = [r for r in reqs if _get(r, "source") == "situation"]
        avoided = [r for r in supplied if _get(r, "state") != "missing"]
        silent = [r for r in avoided if not _needs_ask(r)]
        table_all = [r for r in supplied
                     if isinstance(_get(r, "table_item_id"), str) and _get(r, "table_item_id")]
        table_avoided = [r for r in table_all if _get(r, "state") != "missing"]
        # Computed over table_all, NOT table_avoided: _needs_ask already returns True
        # for every non-"have" state, so restricting to avoided cannot change the
        # answer — but computing over the wider set means a bind that reaches "have"
        # by some future path still trips the wire.
        table_silent = [r for r in table_all if not _needs_ask(r)]
        return {
            "requirements": len(reqs),
            "supplied": len(supplied),
            "avoided_gap": len(avoided),
            "silent": len(silent),
            "prefilled_confirm": len(avoided) - len(silent),
            "table_supplied": len(table_all),
            "table_avoided_gap": len(table_avoided),
            "table_silent": len(table_silent),
            "rate": (len(avoided) / len(supplied)) if supplied else 0.0,
        }
    except Exception:
        logger.debug("[table_payoff] inventory_hit_report failed", exc_info=True)
        # NOTE the asymmetry: every payoff counter degrades to 0, but table_silent
        # degrades to None, NOT to 0. Zero is this tripwire's HEALTHY reading, so
        # returning it on an internal error would be a fail-OPEN alarm that reports
        # "clamp intact" precisely when it failed to look. None means "not evaluated"
        # and clamp_tripwire renders it as such.
        return {"requirements": 0, "supplied": 0, "avoided_gap": 0, "silent": 0,
                "prefilled_confirm": 0, "table_supplied": 0, "table_avoided_gap": 0,
                "table_silent": None, "rate": 0.0}


# ── DEC-25 §5.10.e AC7 — the clamp-regression tripwire ──────────────────────
def clamp_tripwire(report: Any) -> Dict[str, Any]:
    """The healthy-is-ZERO alarm on table-sourced silent binds (DEC-25, §5.10.e AC7).

    Returns ``{"table_silent": int|None, "fired": bool, "evaluated": bool,
    "message": str|None}``.

    ``fired`` is True **only** on a positive count — never on ``None``. The two
    failure modes are reported apart on purpose: ``evaluated=False`` means the metric
    could not be computed and the clamp's state is UNKNOWN, which is not the same
    claim as ``fired=False`` ("looked, and the clamp holds"). Collapsing them would
    make an exception inside the metric read as a clean bill of health.

    This is deliberately NOT a raise. The tripwire is observability: it must never be
    able to fail a run that the clamp itself already handled correctly (the operator
    still got their confirm). It makes a punched clamp LOUD, it does not adjudicate.
    """
    try:
        rep = inventory_hit_report(report)
        n = rep.get("table_silent")
        if not isinstance(n, int):
            return {"table_silent": None, "evaluated": False, "fired": False,
                    "message": "table-clamp tripwire NOT EVALUATED (metric unavailable)"}
        if n <= 0:
            return {"table_silent": 0, "evaluated": True, "fired": False,
                    "message": None}
        return {
            "table_silent": n, "evaluated": True, "fired": True,
            "message": (
                f"CLAMP REGRESSION: {n} table-sourced requirement(s) bound SILENTLY. "
                "This is structurally impossible while IMPL-5 holds "
                "(_entry_origin clamps surveyed entries to content_derived; "
                "_needs_ask always asks for content_derived). Treat as a SECURITY "
                "regression, not as improved table payoff — read table_payoff's "
                "module docstring before changing either helper."
            ),
        }
    except Exception:
        logger.debug("[table_payoff] clamp_tripwire failed", exc_info=True)
        return {"table_silent": None, "evaluated": False, "fired": False,
                "message": "table-clamp tripwire NOT EVALUATED (error)"}


def format_inventory_hit(rep: Dict[str, Any]) -> List[str]:
    """Human-readable metric lines (the §10 'trend, reported' surface).

    The DEC-25 tripwire line is emitted FIRST and BEFORE the "nothing supplied"
    early return. A clamp regression implies ``supplied > 0`` today, so the ordering
    is not load-bearing for the current shape — but an alarm that renders only after
    a payoff precondition passes is an alarm with a mute switch attached to unrelated
    state, and this one must not have one.
    """
    lines: List[str] = []
    try:
        if isinstance(rep, dict) and "table_silent" in rep:
            n = rep["table_silent"]
            if isinstance(n, int) and n > 0:
                lines.append(
                    f"  !! CLAMP REGRESSION — table-sourced silent binds: {n} "
                    f"(healthy is 0; this is a SECURITY regression, not payoff)"
                )
            elif n is None:
                # present-but-None = inventory_hit_report's error path. Distinct from
                # a healthy 0, and must not render as one.
                lines.append("  ?? table-clamp tripwire NOT EVALUATED")

        if not isinstance(rep, dict) or not rep.get("supplied"):
            return lines + ["Inventory-hit: no requirements were supplied by the inventory."]
        return lines + [
            f"Inventory-hit rate: {rep.get('rate', 0.0):.0%} "
            f"({rep.get('avoided_gap', 0)}/{rep.get('supplied', 0)} supplied "
            f"requirements avoided a from-scratch gap)",
            f"  bound with no ask: {rep.get('silent', 0)} · "
            f"pre-filled one-click confirm: {rep.get('prefilled_confirm', 0)}",
            f"  from your table: {rep.get('table_avoided_gap', 0)} of "
            f"{rep.get('table_supplied', 0)}",
        ]
    except Exception:
        return lines + ["Inventory-hit: unavailable."]


# ── §5.10.c the during-task chip row ────────────────────────────────────────
def _item_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or "")
    return str(getattr(item, "name", "") or "")


def _item_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id") or "")
    return str(getattr(item, "id", "") or "")


def using_from_table(report: Any, items: Any = None) -> Dict[str, Any]:
    """The §5.10.c during-task *"using from your table: X · Y"* chip row.

    Returns ``{"chips": [{"id", "name"}...], "overflow": int, "total": int}``.
    Deduped by table item id and capped at :data:`MAX_USING_CHIPS`; the remainder
    is reported as ``overflow`` rather than dropped, so the strip never
    under-reports how much of the run leaned on the table.

    ``items`` (the TableItems) supplies display names. An id with no matching item
    still yields a chip — under its id — because the item may have been removed
    mid-run, and dropping it would make the strip disagree with the metric.
    Never raises; degrades to an empty row.
    """
    try:
        by_id = {}
        for it in (items or []):
            iid = _item_id(it)
            if iid:
                by_id[iid] = _item_name(it)

        seen: set = set()
        ordered: List[Dict[str, str]] = []
        for req in table_backed(report):
            tid = _get(req, "table_item_id")
            if tid in seen:
                continue
            seen.add(tid)
            ordered.append({"id": tid, "name": by_id.get(tid) or tid})

        return {"chips": ordered[:MAX_USING_CHIPS],
                "overflow": max(0, len(ordered) - MAX_USING_CHIPS),
                "total": len(ordered)}
    except Exception:
        logger.debug("[table_payoff] using_from_table failed", exc_info=True)
        return {"chips": [], "overflow": 0, "total": 0}


# ── §5.10.c the completion chip (≤1, novelty-gated) ─────────────────────────
def _novelty_path(vault) -> Path:
    from systemu.runtime.table_store import _dir
    return _dir(vault) / "table_celebrations.json"


def load_celebrations(vault) -> Dict[str, int]:
    """table_item_id → times its contribution has been celebrated. Broken ⇒ {}."""
    try:
        path = _novelty_path(vault)
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): int(v) for k, v in raw.items()
                if isinstance(k, str) and isinstance(v, (int, float))}
    except Exception:
        return {}


def _save_celebrations(vault, data: Dict[str, int]) -> None:
    try:
        from systemu.runtime.table_store import _write_atomic
        if len(data) > MAX_NOVELTY_KEYS:
            for stale in list(data.keys())[: len(data) - MAX_NOVELTY_KEYS]:
                data.pop(stale, None)
        _write_atomic(_novelty_path(vault), json.dumps(data, indent=2))
    except Exception:
        pass


def answered_from_table(report: Any, vault=None, *,
                        record: bool = True) -> Dict[str, Any]:
    """The §5.10.c completion chip — **≤1 per task, novelty-gated**.

    Returns ``{"count": int, "chip": str|None, "suppressed": bool}``.

    ``count`` is the RAW number of table-supplied requirements that avoided a
    from-scratch gap. It is computed BEFORE the novelty gate and is returned
    whether or not the chip renders — §5.10.c is explicit that the raw count still
    feeds the §10 metric when the chip is suppressed, so a caller that reads only
    ``chip`` would silently under-count the metric.

    The gate is per TABLE ITEM, not per task: once every contributing item has been
    celebrated :data:`NOVELTY_CELEBRATION_LIMIT` times the chip stops. An item the
    operator has never seen celebrated keeps the chip alive even in a run that is
    otherwise all-familiar — the chip exists to teach that the table paid off, and
    a brand-new item is exactly what is worth teaching.

    ``record=False`` reads the gate without advancing it (for a preview/render that
    is not the actual completion), so a page refresh cannot burn an item's novelty.
    ``vault=None`` disables persistence: the chip renders and nothing is recorded.
    """
    try:
        rows = [r for r in table_backed(report) if _get(r, "state") != "missing"]
        count = len(rows)
        if count <= 0:
            return {"count": 0, "chip": None, "suppressed": False}

        ids = []
        for r in rows:
            tid = _get(r, "table_item_id")
            if tid not in ids:
                ids.append(tid)

        if vault is None:
            return {"count": count,
                    "chip": f"{count} answered from your table",
                    "suppressed": False}

        seen = load_celebrations(vault)
        novel = [i for i in ids if seen.get(i, 0) < NOVELTY_CELEBRATION_LIMIT]
        if not novel:
            # every contributing item is familiar — suppress the CHIP, keep the COUNT
            return {"count": count, "chip": None, "suppressed": True}

        if record:
            for i in novel:
                seen[i] = seen.get(i, 0) + 1
            _save_celebrations(vault, seen)

        return {"count": count,
                "chip": f"{count} answered from your table",
                "suppressed": False}
    except Exception:
        logger.debug("[table_payoff] answered_from_table failed", exc_info=True)
        return {"count": 0, "chip": None, "suppressed": False}
