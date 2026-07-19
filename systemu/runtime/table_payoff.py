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

**Spec tension, recorded not resolved (§5.10.e AC7).** AC7 reads "a requirement
resolvable from a table item binds without an ask." Under the IMPL-5 clamp that is
unsatisfiable for every table-backed source. This module implements the clamp-
respecting reading. Relaxing ``_entry_origin`` to make AC7 literally true would
convert a security control into a product metric — the wrong trade, and explicitly
warned against in the binder's own docstring. Escalated, not silently reconciled.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _is_ask(req: Any) -> bool:
    """Mirror of ``requirement_binder._needs_ask`` over model-or-dict.

    NOT imported from the binder: that helper takes the binder's own ``_get`` and
    this module must also read snapshot dicts. The logic is one line and pinned by
    ``test_is_ask_tracks_the_binder`` so the two cannot drift apart unnoticed.
    """
    if _get(req, "state") != "have":
        return True
    return _get(req, "value_origin") == "content_derived"


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

    ``rate`` is ``avoided_gap / supplied``, and is ``0.0`` (not a division error,
    not None) when nothing was supplied. Never raises.
    """
    try:
        reqs = _requirements(report)
        supplied = [r for r in reqs if _get(r, "source") == "situation"]
        avoided = [r for r in supplied if _get(r, "state") != "missing"]
        silent = [r for r in avoided if not _is_ask(r)]
        table_all = [r for r in supplied
                     if isinstance(_get(r, "table_item_id"), str) and _get(r, "table_item_id")]
        table_avoided = [r for r in table_all if _get(r, "state") != "missing"]
        return {
            "requirements": len(reqs),
            "supplied": len(supplied),
            "avoided_gap": len(avoided),
            "silent": len(silent),
            "prefilled_confirm": len(avoided) - len(silent),
            "table_supplied": len(table_all),
            "table_avoided_gap": len(table_avoided),
            "rate": (len(avoided) / len(supplied)) if supplied else 0.0,
        }
    except Exception:
        logger.debug("[table_payoff] inventory_hit_report failed", exc_info=True)
        return {"requirements": 0, "supplied": 0, "avoided_gap": 0, "silent": 0,
                "prefilled_confirm": 0, "table_supplied": 0, "table_avoided_gap": 0,
                "rate": 0.0}


def format_inventory_hit(rep: Dict[str, Any]) -> List[str]:
    """Human-readable metric lines (the §10 'trend, reported' surface)."""
    try:
        if not isinstance(rep, dict) or not rep.get("supplied"):
            return ["Inventory-hit: no requirements were supplied by the inventory."]
        return [
            f"Inventory-hit rate: {rep.get('rate', 0.0):.0%} "
            f"({rep.get('avoided_gap', 0)}/{rep.get('supplied', 0)} supplied "
            f"requirements avoided a from-scratch gap)",
            f"  bound with no ask: {rep.get('silent', 0)} · "
            f"pre-filled one-click confirm: {rep.get('prefilled_confirm', 0)}",
            f"  from your table: {rep.get('table_avoided_gap', 0)} of "
            f"{rep.get('table_supplied', 0)}",
        ]
    except Exception:
        return ["Inventory-hit: unavailable."]


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
