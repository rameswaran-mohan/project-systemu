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

SCOPE (W-A final slice, R-W1). The slice-2c deferral is now CLOSED. This module is the
read API + its fence, and it is reached from exactly two live surfaces:

  * :func:`goal_view` — the §5.11.b inversion. ``situational_inventory`` composes a
    goal-conditioned RANKED view over the store into ``SituationReport.world_facts``,
    and ``render_situation_for_prompt`` renders those rows through
    :func:`render_facts_for_prompt`. The report now READS the store (it previously only
    fed it, one-way, via ``world_model_populator``), which is what gives an R-W2 census
    fact — written by a producer the five live inventory sources know nothing about — a
    place to surface.
  * :func:`run_view` — the registered ``world_query`` tool (``runtime/tools/
    world_tools.py``). This is the NEVER-SUBTRACT escape hatch (§5.11 AC4) that makes
    the view's trim legitimate: a fact ranked out of, or staleness-dropped from, the
    composed view is still retrievable on demand.

    Precisely WHO can retrieve it matters and is narrower than §5.11.b's wording
    suggests. The composed view is rendered into the OPEN-WORLD PLANNER prompt, and
    that is a single JSON-response LLM call with no tools; the tool belongs to the
    EXECUTING agent's decision loop. So "trim, because more can be queried" holds
    across the run, not within the planner call. Documented rather than papered over:
    the planner sees a trimmed world it cannot itself widen.

Both go through :func:`fenced_row`, so there is ONE place where a stored fact becomes
prompt-facing bytes and ONE field allowlist to audit.
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


def _row_of(item: Any, survey: Optional[Any] = None) -> dict:
    """One fenced row from EITHER a live :class:`Fact` OR an already-composed row dict.

    The dict branch exists because the §5.11.b inversion composes its rows at SURVEY
    time — the only moment the survey watermark is in hand — and then carries them on
    ``SituationReport.world_facts`` through a ``model_dump()`` and, on a resume, through
    a persisted-then-deserialized snapshot. Re-rendering such a row must therefore treat
    it as UNTRUSTED INPUT, not as something this module produced:

      * the field ALLOWLIST is re-applied, so a poisoned snapshot cannot smuggle an
        extra key (``origin_class``, ``confidence``, an instruction-shaped field) into
        the prompt by having put it on the row;
      * ``bind_taint`` is RE-DERIVED, never copied. A row asserting
        ``bind_taint="operator"`` is exactly the laundering :func:`bind_taint_of`
        exists to make impossible, and a copy would honour it.

    ``staleness`` IS carried over on the dict branch — it is the one field that cannot
    be recomputed here (it needs the watermark from the survey that produced the row),
    and it is advisory-only: it never gates a bind, so a forged value costs at most a
    misleading freshness label, whereas recomputing it without a watermark would stamp
    every carried row ``unknown`` and destroy the signal for every honest one."""
    if isinstance(item, dict):
        row = {k: item.get(k) for k in FENCED_ROW_FIELDS}
        row["bind_taint"] = bind_taint_of(item)     # re-derived, never copied
        return row
    return fenced_row(item, survey)


def render_facts_for_prompt(facts: List[Any], *, query: str = "",
                            survey: Optional[Any] = None) -> str:
    """Render world.query results as a FENCED, deterministic JSON block (WM-15).

    Results describe WHAT EXISTS; they are never instructions. Uses the same
    ``situational_inventory.fence`` (nonce'd, delimiter-neutralising, fail-closed) that
    already wraps the SituationReport, so there is ONE fence implementation to audit.

    Accepts live ``Fact`` objects (the tool path) or pre-composed rows (the report
    path) — see :func:`_row_of`."""
    try:
        body = json.dumps(
            {"query": str(query or ""),
             "results": [_row_of(f, survey) for f in (facts or [])]},
            sort_keys=True, default=str)
    except Exception:
        body = json.dumps({"query": str(query or ""), "results": []}, sort_keys=True)
    return fence(body)


def render_provenance_for_prompt(fact_id: str, steps: Optional[List[Any]]) -> str:
    """Render WM-4 ``provenance(fact_id)`` — the fact's append-only ``source_chain``.

    ``None`` (unknown fact) renders as an explicit ``null`` rather than an empty list:
    "we have no such fact" and "we have it but recorded no provenance" are different
    answers, and collapsing them would let an unknown id read as a provenance-free fact.
    Fenced like every other world-model read; ids/handles only, never a secret (E6)."""
    if steps is None:
        return fence(json.dumps({"fact_id": str(fact_id or ""), "provenance": None},
                                sort_keys=True))
    try:
        body = json.dumps({"fact_id": str(fact_id or ""), "provenance": [
            {"source_kind": str(getattr(s, "source_kind", "") or ""),
             "ref": str(getattr(s, "ref", "") or ""),
             "at": str(getattr(s, "at", "") or "")}
            for s in steps]}, sort_keys=True, default=str)
    except Exception:
        body = json.dumps({"fact_id": str(fact_id or ""), "provenance": None},
                          sort_keys=True)
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


# ── §5.11.b: the SituationReport as a goal-conditioned ranked VIEW ────────────

#: How many fact rows the composed report view may carry into the planner prompt.
#: Small ON PURPOSE. This is a CONTEXT budget, not a knowledge budget: the whole
#: never-subtract argument (§5.10.d, bound to the STORE) is that a view may rank and
#: trim BECAUSE the ``world_query`` tool can reach past it. Raising this without the
#: tool registered would be the unsound direction; lowering it costs nothing but a
#: tool call.
DEFAULT_VIEW_LIMIT = 12

#: The default cap on ONE ``world_query`` tool answer. Larger than the composed view
#: (the point of the escape hatch is to see more), still bounded so a large store
#: cannot flood the context in a single call.
DEFAULT_QUERY_LIMIT = 30


def goal_view(vault: Any, goal: str = "",
              limit: int = DEFAULT_VIEW_LIMIT) -> List[dict]:
    """The goal-conditioned ranked view over the fact store, as FENCED ROWS.

    Composed at survey time by ``situational_inventory.compose_world_view``. Two
    deliberate properties:

    **Ranked, not filtered.** Uses :func:`world_model.ranked_view`, which orders the
    WHOLE store by goal overlap rather than dropping zero-overlap facts — a goal worded
    differently from the operator's setup must yield a low-ranked world, never an empty
    one.

    **``unconfirmed`` facts are dropped from the view.** That is the ONE staleness class
    where the latest survey genuinely covered the fact's scope and did not re-see it —
    the honest "this may be gone" signal. Carrying a revoked root's path or a
    disconnected service into the planner prompt is the concrete way a durable store
    misleads planning (§5.11 honest-risk 3), and it is also the only class the store adds
    that the live report would not have shown anyway. ``not_surveyed`` rows are KEPT:
    absence of coverage is not evidence, and it is exactly the class an R-W2 census fact
    lands in (the inventory survey never covers a census kind). Dropped rows stay
    reachable through the ``world_query`` tool — that is what makes the drop a view
    decision rather than a subtraction.

    FAIL-SAFE: never raises. An absent, empty or broken store yields ``[]``, which is
    byte-identical downstream to the feature being absent (§5.11.f risk-5)."""
    try:
        store = _wm.FactStore(vault)
        survey = store.latest_survey()
        rows: List[dict] = []
        cap = max(0, int(limit))
        for f in _wm.ranked_view(store, str(goal or "")):
            if len(rows) >= cap:
                break
            try:
                if _wm.staleness_of(f, survey) == "unconfirmed":
                    continue
                rows.append(fenced_row(f, survey))
            except Exception:
                continue                       # one bad fact never empties the view
        return rows
    except Exception:
        logger.debug("[world-model] goal view skipped (non-fatal)", exc_info=True)
        return []


# ── WM-4: the registered ``world.query`` tool family ─────────────────────────

#: The five §5.11.b views, exposed through ONE registered tool's ``view`` enum rather
#: than five tool entries. The family is complete — every view is named and reachable —
#: and one entry costs the model's context far less than five near-identical schemas.
VIEWS = ("find_services", "what_can", "find_data", "about", "provenance")


class UnknownViewError(ValueError):
    """Raised for a ``view`` outside :data:`VIEWS`, or for a view invoked without the
    arguments it needs. NEVER silently substituted with a default view: answering a
    question the caller did not ask, from a store the caller cannot see, is the
    "accept the input and do something different" failure — a wrong world model is
    supposed to cost an ask, not a confident wrong answer."""


def run_view(vault: Any, view: str, *, query: str = "", verb: str = "",
             target_class: str = "", under: str = "",
             limit: int = DEFAULT_QUERY_LIMIT) -> dict:
    """Run one WM-4 view and return ``{"view", "count", "fenced"}``.

    ``fenced`` is the prompt-safe payload (WM-15) — the ONLY thing a caller should put
    in front of a model. ``count`` is metadata about the answer, not part of it.

    Raises :class:`UnknownViewError` for an unknown view or a missing required argument.
    Every other failure is the store's problem and degrades to an EMPTY result, because
    "the store is unreadable" must read as a smaller world, never as a crash on a
    read-only query."""
    name = str(view or "").strip()
    if name not in VIEWS:
        raise UnknownViewError(
            f"unknown world.query view {view!r} — valid views: {', '.join(VIEWS)}")
    cap = max(1, int(limit or DEFAULT_QUERY_LIMIT))
    store = _wm.FactStore(vault)
    survey = store.latest_survey()

    if name == "provenance":
        fact_id = str(query or "").strip()
        if not fact_id:
            raise UnknownViewError("provenance requires `query` = the fact_id to explain")
        steps = _wm.provenance(store, fact_id)
        return {"view": name, "count": (0 if steps is None else len(steps)),
                "fenced": render_provenance_for_prompt(fact_id, steps)}

    if name == "what_can":
        v, t = str(verb or "").strip(), str(target_class or "").strip()
        if not v or not t:
            raise UnknownViewError(
                "what_can requires BOTH `verb` and `target_class` (e.g. verb='create', "
                "target_class='issue')")
        facts = _wm.what_can(store, v, t, limit=cap)
        label = f"what_can({v},{t})"
    else:
        term = str(query or "").strip()
        if not term:
            raise UnknownViewError(f"{name} requires a non-empty `query`")
        if name == "find_services":
            facts = _wm.find_services(store, term, limit=cap)
        elif name == "find_data":
            facts = _wm.find_data(store, term, under=(str(under or "").strip() or None),
                                  limit=cap)
        else:                                    # about
            facts = _wm.about(store, term, limit=cap)
        label = f"{name}({term})"

    return {"view": name, "count": len(facts),
            "fenced": render_facts_for_prompt(facts, query=label, survey=survey)}
