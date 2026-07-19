"""Insights — tabbed parent page hosting Memory, Flywheel, and Events panels.

v0.7.2 sidebar consolidation: three small read-only analytics surfaces that
used to live as separate top-level routes (/memory, /flywheel,
/notifications) are now tabs inside one /insights destination.  The
underlying page builders are unchanged — this module only composes them.

Phase 5 Slice 4d: the former "actions" tab (a 4th OperatorDecision surface)
is REMOVED.  Decisions now live in EXACTLY one place — the Inbox (right rail
+ /inbox page + Console mini-pane).  ``/insights?tab=actions`` deep-links
redirect to /inbox so bookmarks + notification links don't 404.  The shared
helpers ``_build_pending_decision_view_model`` and ``render_decision_card``
remain — the Console pending-actions mini-pane imports the latter.

Direct-link to a specific tab via ``/insights?tab=memory|flywheel|events``.
The legacy URLs (/memory, /flywheel, /notifications) are preserved as
redirect handlers in ``dashboard.py`` so bookmarks + notification deep
links continue to work.
"""

from __future__ import annotations

import logging

from nicegui import ui

from systemu.interface.dashboard_state import THEME
from systemu.interface.pages.flywheel_page import build_flywheel_page
from systemu.interface.pages.memory_consolidation_page import (
    build_memory_consolidation_page,
)
from systemu.interface.pages.notifications_page import build_notifications_page

logger = logging.getLogger(__name__)

_VALID_TABS = ("memory", "flywheel", "events")

#: Sentinel returned by ``_resolve_tab`` when the requested tab is the removed
#: decision-surface ("actions") — the caller redirects to /inbox instead.
REDIRECT_INBOX = "REDIRECT_INBOX"


def _resolve_tab(tab: str | None) -> str:
    """Pure tab-normalizer for the Insights query string.

    Returns one of ``_VALID_TABS``, or the ``REDIRECT_INBOX`` sentinel when
    the caller asked for the removed ``actions`` decision-surface (Slice 4d).
    Anything else (unknown / empty / None) normalizes to the default tab so a
    stale deep-link lands somewhere sensible instead of 404-ing.
    """
    if tab == "actions":
        return REDIRECT_INBOX
    if tab in _VALID_TABS:
        return tab
    return "memory"


def _tab_url(tab: str) -> str:
    """Pure: the shareable deep-link URL for an Insights tab.

    Mirrors the redirect targets in dashboard.py (``/insights?tab=…``) so the
    load-direction (URL → tab) and the click-direction (tab → URL) agree.
    """
    return f"/insights?tab={tab}"


def build_structured_answer(questions: list, values: dict) -> str:
    """v0.8.19 — serialize structured-question answers to the JSON stored as choice."""
    import json
    return json.dumps({q["id"]: values.get(q["id"]) for q in (questions or [])})


def build_elicitation_answer(schema: dict, values: dict, picked=None) -> str:
    """v0.9.35 (P1) — serialize an elicitation form's per-field values to the
    JSON stored as the decision choice. Only fields declared in the schema's
    properties are emitted (secret fields never enter this dict). The reconciler
    type-coerces these strings via elicitation.param_answers_from_choice.

    R-B4/F3: ``picked`` is the set of field names for which the operator
    explicitly chose the offered suggestion rather than typing their own value.
    It rides alongside the values under ``PICK_MARKER_KEY`` — the one place in
    the system that KNOWS the answer's provenance instead of inferring it from a
    digest comparison. Intersected with the declared properties so the marker can
    only ever name a real field, and omitted entirely when nothing was picked (so
    an untouched card serializes exactly as before).
    """
    import json
    from systemu.runtime.elicitation import PICK_MARKER_KEY
    props = (schema or {}).get("properties") or {}
    out = {name: (values or {}).get(name) for name in props}
    chosen = sorted(set(picked or []) & set(props))
    if chosen:
        out[PICK_MARKER_KEY] = chosen
    return json.dumps(out)


def answer_ack_model(vault, request_id: str) -> dict:
    """The §5.6 "on your table ✓" acknowledgement for one ask.

    Returns ``{"visible": bool, "rows": [{"key","name","kind"}], "text": str}``.

    Pure read of the receipt `ask_promotion` wrote when the answer materialized a
    learned TableItem — this NEVER re-derives what a card would have been, so the
    chip can only ever claim something that was actually written.

    ``visible`` is False on any failure, and on an empty receipt. The failure
    direction matters: a missing acknowledgement is a chip the operator does not
    see, while a fabricated one is systemu telling them it did something it did
    not. An ACKNOWLEDGEMENT, never a gate (§5.6) — nothing here blocks the answer.
    """
    try:
        if vault is None or not request_id:
            return {"visible": False, "rows": [], "text": ""}
        from systemu.runtime.table_store import load_answer_receipts
        rows = load_answer_receipts(vault).get(str(request_id), [])
    except Exception:
        return {"visible": False, "rows": [], "text": ""}
    if not rows:
        return {"visible": False, "rows": [], "text": ""}
    names = ", ".join(r["name"] for r in rows if r.get("name"))
    return {
        "visible": True,
        "rows": rows,
        "text": (f"On your table: {names}" if names
                 else f"{len(rows)} item(s) added to your table"),
    }


def build_insights_page(default_tab: str = "memory") -> None:
    """Render the Insights page with three tabs (Memory / Flywheel / Events).

    Args:
        default_tab: Which tab is active on load.  Falls back to ``memory``
                     when the query string supplies anything unrecognised.
                     ``?tab=actions`` (the removed decision-surface) redirects
                     to /inbox — decisions live only in the Inbox now.
    """
    resolved = _resolve_tab(default_tab)
    if resolved == REDIRECT_INBOX:
        ui.navigate.to("/inbox")
        return
    default_tab = resolved

    ui.label("Insights").style(
        f"font-size: 28px; font-weight: 800; color: {THEME['text']}; margin-bottom: 4px;"
    )
    from systemu.interface.design.glossary import lore_sublabel
    ui.label(lore_sublabel("insights")).style(
        f"color: {THEME['text_muted']}; font-size: 14px; margin-bottom: 20px;"
    )

    # ── Tabs header ─────────────────────────────────────────────────────────
    with ui.tabs().style(
        f"background: {THEME['surface']}; border-bottom: 1px solid {THEME['border']};"
    ) as tabs:
        ui.tab("memory", label="Memory")
        ui.tab("flywheel", label="Flywheel")
        ui.tab("events", label="Manual Logs")

    # Deep-link round-trip: clicking a tab rewrites the URL (no reload) so the
    # active tab is shareable/bookmarkable — the mirror of the ?tab= load path.
    def _sync_tab_url(e) -> None:
        val = getattr(e, "value", None)
        if val in _VALID_TABS:
            ui.run_javascript(f"history.replaceState(null, '', {_tab_url(val)!r})")

    tabs.on_value_change(_sync_tab_url)

    # ── Tab panels ──────────────────────────────────────────────────────────
    # Each panel calls the existing page builder verbatim — no logic moves.
    with ui.tab_panels(tabs, value=default_tab).classes("w-full").style(
        "padding-top: 16px;"
    ):
        with ui.tab_panel("memory"):
            build_memory_consolidation_page()
        with ui.tab_panel("flywheel"):
            build_flywheel_page()
        with ui.tab_panel("events"):
            # v0.8.16: render the live, origin-filtered Manual Logs feed at the
            # top so the full-page tab matches the Console mini-pane (capture /
            # manual / scheduled).  The existing pending-notifications file-tail
            # content stays below — this is purely additive.
            from systemu.interface.components.live_events_pane import (
                build_supervisor_events_pane,
            )
            ui.label("Live Manual Logs").style(
                f"font-size: 14px; font-weight: 700; color: {THEME['text']}; "
                f"margin-bottom: 8px;"
            )
            build_supervisor_events_pane(
                origins=frozenset({"capture", "manual", "scheduled"})
            )
            ui.separator().style("margin: 16px 0;")
            build_notifications_page()


def _build_pending_decision_view_model(vault):
    """Pure-data helper — testable without NiceGUI runtime.

    Returns a list of dicts:
        [{"id":, "title":, "body":, "options":, "context":, "dedup_key":}]
    OR one of the sentinels:
        {"_no_vault": True}
        {"_empty": True}
        {"_error": str}
    """
    if vault is None:
        return {"_no_vault": True}
    from systemu.approval.decision_queue import OperatorDecisionQueue
    queue = OperatorDecisionQueue(vault)
    try:
        pending = queue.list_pending()
    except Exception as exc:
        return {"_error": str(exc)}
    if not pending:
        return {"_empty": True}
    return [
        {
            "id": d.id,
            "title": d.title,
            "body": d.body,
            "options": list(d.options),
            "context": dict(d.context),
            "dedup_key": d.dedup_key,
        }
        for d in pending
    ]


def _json_safe(obj):
    """JSON round-trip with ``default=str`` so any stray non-serializable value
    (e.g. a callable) degrades to its repr instead of crashing orjson inside the
    NiceGUI outbox (``Outbox._emit``). Mirrors the v0.9.45 Context-expansion
    sanitize — now the single definition both json_editor payloads reuse."""
    import json as _json
    return _json.loads(_json.dumps(obj, default=str))


def sanitize_card_payload(card: dict) -> dict:
    """Defect-E defense-in-depth: deep-coerce a decision-card dict to JSON-safe
    values so NO field (context / spec / options / …) can carry a callable into a
    NiceGUI element prop and crash ``Outbox._emit`` -> orjson with
    ``Type ... is not JSON serializable: function``."""
    if not isinstance(card, dict):
        return card
    return _json_safe(card)


def _renderable_options(options):
    """IMPL-2: drop options this renderer cannot actually deliver.

    Returns ``(kept, suppressed)``.

    ``render_decision_card`` draws every option as a plain button wired to
    ``queue.resolve(id, choice=label)``. That is the wrong shape for
    "Reclassify effect…": the remedy needs a panel (pick a class, transcribe it to
    confirm) and resolves through ``resolve_with_context_patch`` with the assigned
    class attached. Rendered here it would resolve the card, record NOTHING, and
    re-DENY — the operator would see the remedy, use it, and land back in exactly the
    dead end IMPL-2 exists to remove, on a surface that looks identical to the Inbox.

    Suppressing it is the conservative half of the fix: the option simply is not
    offered where it cannot work, and the card's own body already points at the Inbox.
    Never returns an empty list — a decision card with no buttons is unresolvable.
    """
    from systemu.interface.command.gate import RECLASSIFY_OPTION
    original = list(options or [])
    kept = [o for o in original if o != RECLASSIFY_OPTION]
    suppressed = len(kept) != len(original)
    if not kept:
        kept = ["Deny"]
    return kept, suppressed


def render_decision_card(card: dict, queue, on_resolved) -> None:
    """Render one OperatorDecision card with working resolve+dispatch buttons.

    Shared by the /insights Pending Actions tab and the v0.8.8 Console
    pending-actions mini-pane so the resolve + v0.8.5 dispatch behavior is
    defined in exactly one place.

    Args:
        card: dict with keys id, title, body, options, context, dedup_key
              (as produced by ``_build_pending_decision_view_model``).
        queue: ``OperatorDecisionQueue`` instance used to resolve the choice.
        on_resolved: no-arg callback invoked after a successful resolve — each
                     surface passes its own ``@ui.refreshable`` ``.refresh``
                     so the resolved card drops in-place without a full reload.
    """
    # Defect-E: make the whole card JSON-safe before ANY element is built, so a
    # stray callable in context/spec can't crash the NiceGUI outbox for everyone
    # on the page. Belt-and-suspenders with the per-json_editor sanitize below.
    card = sanitize_card_payload(card)
    with ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 12px; padding: 16px; margin-bottom: 12px;"
    ):
        ui.label(card["title"]).style(
            f"font-size: 16px; font-weight: 600; color: {THEME['text']};"
        )
        ui.label(card["body"]).style(
            f"font-size: 13px; color: {THEME['text_muted']}; margin-bottom: 12px;"
        )
        if card["dedup_key"]:
            ui.label(f"dedup: {card['dedup_key']}").style(
                f"font-size: 11px; color: {THEME['text_muted']}; "
                f"font-family: monospace; margin-bottom: 8px;"
            )

        # R-UX3 / UX-14: the "why?" expansion. This is the ONE shared card
        # renderer, so attaching here gives every gate/ask card the affordance.
        # Read-only: it renders the persisted record and re-scores nothing.
        try:
            from systemu.interface.components.why_panel import build_why_panel
            build_why_panel(card)
        except Exception:
            pass  # an explanation failing must never break the decision card

        def _make_handler(did, dedup_key, label, the_queue):
            def _click(_e=None):
                try:
                    the_queue.resolve(did, choice=label)
                    ui.notify(f"Resolved: {label}", type="positive")
                    on_resolved()
                    # v0.8.5: dispatch the resolved choice to the registered
                    # pipeline handler in a worker thread so the UI event loop
                    # isn't blocked on multi-second LLM continuations.
                    from systemu.approval.decision_dispatcher import dispatch
                    from systemu.interface.dashboard_state import AppState
                    _state = AppState.get()
                    if _state.config and _state.vault:
                        import threading
                        def _dispatch_resolved():
                            try:
                                decision = _state.vault.get_decision(did)
                            except Exception:
                                logger.exception(
                                    "[Insights] could not load decision %s for dispatch", did,
                                )
                                return
                            dispatch(decision, label, _state.config, _state.vault)
                        # daemon=True trades off shutdown cleanup for unblocked exits:
                        # if the dashboard process exits while this thread is mid-LLM-call,
                        # the thread is killed without finishing the vault write. The
                        # idempotency guard in shadow_decision.decide_shadow (which checks
                        # for an already-assigned shadow on retry) covers the most common
                        # crash-between-saves case, but a kill *during* save_shadow JSON
                        # serialization is not covered. Acceptable for v0.8.5 because the
                        # hourly_shadow_sweep will reconcile any partial state on restart.
                        threading.Thread(
                            target=_dispatch_resolved,
                            daemon=True,
                        ).start()
                except Exception as exc:
                    ui.notify(f"Failed to resolve: {exc}", type="negative")
            return _click

        # v0.9.35 (P1): elicitation form — ONE card, ALL fields, typed widgets,
        # defaults pre-filled, client-side validation, secret fields → URL mode.
        # Keyed on a non-empty requested_schema so non-INPUT gates are untouched.
        _ctx = card.get("context") or {}

        # ── §5.6 "on your table ✓" acknowledgement + undo chip ─────────────────
        # Rendered whenever a receipt exists for this ask. NOTE the timing this
        # cannot escape: promotion runs in the harness-grant reconciler AFTER the
        # decision resolves, so the chip appears on a card RE-RENDERED after that
        # tick (a resumed/re-opened ask, or a later visit), not instantly on
        # submit. It is an acknowledgement, never a gate, so appearing late is a
        # weaker version of the feature rather than a broken one — and claiming
        # "added to your table" before the write had happened would be the
        # dishonest alternative.
        _ack_vault = None
        try:
            from systemu.interface.dashboard_state import AppState
            _ack_vault = getattr(AppState.get(), "vault", None)
        except Exception:
            _ack_vault = None
        _ack = answer_ack_model(_ack_vault, _ctx.get("request_id") or "")
        if _ack["visible"]:
            def _undo_ack(_e=None, rid=_ctx.get("request_id") or "", v=_ack_vault):
                try:
                    from systemu.runtime.table_store import undo_answer_receipt
                    removed = undo_answer_receipt(v, rid)
                except Exception:
                    ui.notify("Couldn't undo that.", type="negative")
                    return
                ui.notify(
                    f"Removed {len(removed)} item(s) from your table. "
                    "Your answer still stands.",
                    type="info", multi_line=True)
                on_resolved()

            with ui.row().classes("items-center s-card q-pa-sm").style("gap: 8px;"):
                ui.icon("check_circle", size="sm").props("color=positive")
                ui.label(_ack["text"]).classes("s-muted")
                ui.button("Undo", on_click=_undo_ack) \
                    .props("flat dense size=sm color=primary")

        _req_schema = _ctx.get("requested_schema") or {}
        if isinstance(_req_schema, dict) and (_req_schema.get("properties")):
            from systemu.runtime.elicitation import is_secret_field
            _props = _req_schema.get("properties") or {}
            _required = _req_schema.get("required") or []
            _widgets: dict = {}
            # field name -> (pick radio, the suggested value) for fields that got
            # the R-B4/F3 explicit-pick control
            _picks: dict = {}
            ui.label(card.get("title") or "Input needed").style(
                f"font-weight: 700; color: {THEME['text']}; margin-bottom: 6px;"
            )
            if card.get("body"):
                ui.label(card["body"]).style(
                    f"font-size: 13px; color: {THEME['text_muted']}; margin-bottom: 8px;"
                )
            for _name, _spec in _props.items():
                _field = {"name": _name, **(_spec if isinstance(_spec, dict) else {})}
                _ftype = (_spec or {}).get("type", "string")
                _desc = (_spec or {}).get("description", "") or _name
                _default = (_spec or {}).get("default")
                ui.label(f"{_name} — {_desc}").style(
                    f"font-size: 13px; color: {THEME['text']}; margin-top: 8px;"
                )
                if is_secret_field(_field):
                    # Secret → URL mode: never a typed input, never in the form/log.
                    ui.label(
                        "🔒 This is a credential. Provide it out-of-band "
                        "(it is never typed here, sent to the model, or logged)."
                    ).style(f"font-size: 12px; color: {THEME['text_muted']};")
                    continue
                _enum = (_spec or {}).get("enum")
                if isinstance(_enum, list) and _enum:
                    _w = ui.select(list(_enum),
                                   value=_default if _default in _enum else None
                                   ).style("min-width: 220px;")
                elif _ftype == "boolean":
                    _w = ui.radio(["Yes", "No"],
                                  value=("Yes" if _default else "No")
                                  if _default is not None else None).props("inline")
                elif _default not in (None, ""):
                    # ── R-B4/F3: a REAL pick ─────────────────────────────────
                    # A candidate used to arrive only as a pre-filled input, which
                    # made "did they take the suggestion?" unanswerable except by
                    # comparing digests after the fact — and that comparison is
                    # dead for path-shaped values, failing toward TRUSTED. Offering
                    # the candidate as an explicit choice means the answer carries
                    # its own provenance and nothing has to be inferred.
                    #
                    # The free-text box stays live either way: this is a pick, not
                    # a closed enum. Making it an enum would be the wrong fix — it
                    # would stop the operator answering anything the binder had not
                    # already guessed.
                    _pick_w = ui.radio(
                        ["Use this suggestion", "Enter my own"],
                        value="Use this suggestion",
                    ).props("inline")
                    ui.label(f"suggested: {_default}").classes("s-muted").style(
                        "font-size: 12px;")
                    _w = ui.input(_name, value=str(_default)).style("min-width: 220px;")
                    _picks[_name] = (_pick_w, str(_default))
                    _widgets[_name] = (_ftype, _w)
                    continue
                else:
                    _w = ui.input(_name, value=("" if _default is None else str(_default))
                                  ).style("min-width: 220px;")
                _widgets[_name] = (_ftype, _w)

            def _submit_form(_e=None, did=card["id"], the_queue=queue,
                             schema=_req_schema, widgets=_widgets, required=_required,
                             picks=_picks):
                try:
                    vals: dict = {}
                    for _n, (_t, _wg) in widgets.items():
                        _v = getattr(_wg, "value", None)
                        if _t == "boolean":
                            _v = (_v == "Yes") if _v in ("Yes", "No") else None
                        vals[_n] = _v
                    # R-B4/F3 — a field counts as PICKED only when the operator left
                    # the radio on "use this suggestion" AND the box still holds that
                    # exact value. Editing the text after picking is an override, and
                    # the stricter of the two signals is the honest one: this must not
                    # stamp "the operator confirmed our candidate" on a value they
                    # then changed.
                    picked = [
                        _n for _n, (_pw, _sv) in picks.items()
                        if getattr(_pw, "value", None) == "Use this suggestion"
                        and str(vals.get(_n)) == _sv
                    ]
                    # Client-side required check (secrets excluded from widgets).
                    _missing = [r for r in required
                                if r in widgets and (vals.get(r) in (None, ""))]
                    if _missing:
                        ui.notify("Fill all required fields: " + ", ".join(_missing),
                                  type="warning")
                        return
                    the_queue.resolve(
                        did, choice=build_elicitation_answer(schema, vals, picked))
                    ui.notify("Submitted", type="positive")
                    on_resolved()
                except Exception as exc:
                    ui.notify(f"Failed: {exc}", type="negative")

            with ui.row().style("gap: 8px; margin-top: 12px;"):
                ui.button("Submit", on_click=_submit_form).style(
                    f"background: {THEME['primary']}; color: white; border-radius: 6px; "
                    f"padding: 6px 14px; font-size: 13px;"
                )
                # Decline = safe default → resolve with the gate's safe_default
                # ("Deny") so _apply_harness_grant emits harness_grant_failed.
                ui.button(
                    "Decline",
                    on_click=_make_handler(card["id"], card["dedup_key"],
                                           card["options"][0] if card["options"] else "Deny",
                                           queue),
                ).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']}; "
                    f"border: 1px solid {THEME['border']}; border-radius: 6px; "
                    f"padding: 6px 14px; font-size: 13px;"
                )
            return  # elicitation branch handled — skip flat option buttons

        # v0.8.19 (R3): structured ask-user cards render option pickers + free
        # text + a single Submit that serializes answers to JSON and resolves
        # the decision with that JSON as the choice.  Returning early skips the
        # flat option buttons + the Context expansion below.
        _qs = (card.get("context") or {}).get("questions")
        if (card.get("context") or {}).get("kind") == "structured_question" and _qs:
            _values: dict = {}
            for _q in _qs:
                ui.label(_q.get("prompt", "")).style(
                    f"font-size: 13px; color: {THEME['text']}; margin-top: 6px;"
                )
                _opts = [o.get("label") for o in _q.get("options", []) if o.get("label")]
                if _opts:
                    if _q.get("multi"):
                        _sel = ui.select(_opts, multiple=True).style("min-width: 220px;")
                    else:
                        _sel = ui.radio(_opts).style("")
                    _values[_q["id"]] = _sel
                if _q.get("allow_free_text"):
                    _txt = ui.input("Other / free text").style("min-width: 220px;")
                    _values[_q["id"] + "__free"] = _txt

            def _submit(_e=None, did=card["id"], the_queue=queue, qs=_qs, vals=_values):
                try:
                    answers = {}
                    for q in qs:
                        sel = vals.get(q["id"])
                        free = vals.get(q["id"] + "__free")
                        ftext = (free.value or "").strip() if free is not None else ""
                        answers[q["id"]] = ftext or (sel.value if sel is not None else None)
                    the_queue.resolve(did, choice=build_structured_answer(qs, answers))
                    ui.notify("Submitted", type="positive")
                    on_resolved()
                except Exception as exc:
                    ui.notify(f"Failed: {exc}", type="negative")

            ui.button("Submit", on_click=_submit).style(
                f"background: {THEME['primary']}; color: white; border-radius: 6px; "
                f"padding: 6px 14px; font-size: 13px; margin-top: 8px;"
            )
            return   # structured branch handled — skip the flat option buttons

        # Amend-then-approve: a harness CAPABILITY gate (not INPUT) gets Deny /
        # Approve / Edit. "Edit" reveals a JSON editor of the spec; submit runs the
        # deterministic safety+band check and resolves as "Approve" with the amended
        # spec stamped. Logic lives in harness_spec_edit (tested) — this is wiring.
        _hctx = card.get("context") or {}
        _hkind = str(_hctx.get("harness_kind") or "").lower()
        if _hctx.get("gate_type") == "harness" and _hkind and _hkind != "input":
            import json as _json
            from systemu.runtime.harness_spec_edit import (
                spec_edit_view, validate_amended_spec, evaluate_amendment,
            )
            from systemu.interface.dashboard_state import AppState
            # Defect-E: sanitize the spec before it seeds the JSON editor props —
            # a callable surviving in context["spec"] would crash the outbox.
            _orig_spec = _json_safe(_hctx.get("spec") or {})
            _edit_state = {"content": {"json": dict(_orig_spec)}}
            _panel = ui.column().style("gap: 8px; margin-top: 8px;")
            _panel.visible = False

            with _panel:
                ui.label("Edit spec (JSON) — Approve grants the amended spec")
                ui.json_editor({"content": {"json": dict(_orig_spec)}},
                               on_change=lambda e: _edit_state.update(content=e.content))

                def _submit_amended(_e=None, did=card["id"], kind=_hkind,
                                    orig=dict(_orig_spec), ctx=_hctx):
                    content = _edit_state.get("content") or {}
                    if "json" in content:
                        edited = content["json"]
                    else:
                        try:
                            edited = _json.loads(content.get("text") or "{}")
                        except Exception:
                            ui.notify("Invalid JSON — fix and retry", type="warning")
                            return
                    if not isinstance(edited, dict):
                        ui.notify("Spec must be a JSON object", type="warning")
                        return
                    errs = validate_amended_spec(kind, edited, original_spec=orig)
                    if errs:
                        ui.notify("; ".join(errs), type="warning")
                        return
                    _state = AppState.get()
                    res = evaluate_amendment(
                        kind=kind, original_spec=orig, edited_spec=edited,
                        arb_context=ctx.get("arb_context") or {},
                        config=getattr(_state, "config", None),
                    )
                    if res["blocked"]:
                        ui.notify(f"Blocked: {res['reason']}", type="negative")
                        return
                    patch = {"amended_spec": edited}
                    if res["band_increase"]:
                        # Typed re-confirmation for a risk-raising edit.
                        with ui.dialog() as _dlg, ui.card():
                            ui.label(f"This edit raises risk {res['from_band']} → "
                                     f"{res['to_band']}. Type CONFIRM to approve.")
                            _ci = ui.input("Type CONFIRM")

                            def _do_confirm():
                                if (_ci.value or "").strip() != "CONFIRM":
                                    ui.notify("Type CONFIRM to proceed", type="warning")
                                    return
                                patch["amend_band_escalation"] = {
                                    "from": res["from_band"], "to": res["to_band"],
                                    "confirmed": True}
                                _dlg.close()
                                queue.resolve_with_context_patch(
                                    did, choice="Approve", context_patch=patch)
                                ui.notify("Approved (amended)", type="positive")
                                on_resolved()
                            ui.button("Confirm", on_click=_do_confirm)
                            ui.button("Cancel", on_click=_dlg.close)
                        _dlg.open()
                        return
                    queue.resolve_with_context_patch(
                        did, choice="Approve", context_patch=patch)
                    ui.notify("Approved (amended)", type="positive")
                    on_resolved()

                ui.button("Approve amended", on_click=_submit_amended)

            with ui.row().style("gap: 8px;"):
                ui.button("Deny", on_click=_make_handler(
                    card["id"], card["dedup_key"], "Deny", queue))
                ui.button("Approve", on_click=_make_handler(
                    card["id"], card["dedup_key"], "Approve", queue))
                ui.button("Edit", on_click=lambda: _panel.set_visibility(True))
            return  # capability-gate branch handled — skip the flat-options loop

        # IMPL-2: this renderer has no reclassify panel, so it must not offer the
        # option — a click here would resolve (and burn) the card while recording
        # nothing. Point the operator at the surface that can actually do it.
        _opts, _suppressed = _renderable_options(card["options"])
        if _suppressed:
            # Token classes, not an inline f-string style — this file already carries
            # 17 baselined inline-style violations and must not grow another.
            ui.label(
                "This action was refused because its effect could not be classified. "
                "To assign the real effect class, open the Inbox — the reclassify "
                "step needs a typed confirmation that this view cannot collect."
            ).classes("s-muted s-cell")
        with ui.row().style("gap: 8px;"):
            for opt in _opts:
                ui.button(
                    opt,
                    on_click=_make_handler(card["id"], card["dedup_key"], opt, queue),
                ).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']}; "
                    f"border: 1px solid {THEME['border']}; border-radius: 6px; "
                    f"padding: 6px 14px; font-size: 13px;"
                )

        if card["context"]:
            with ui.expansion("Context", icon="info").style("margin-top: 10px;"):
                # v0.9.45: sanitize the context (default=str) before json_editor —
                # NiceGUI serializes it to the browser, and a stray callable in the
                # context raised "Type is not JSON serializable: function" (the
                # recurring harness-card render error).
                import json as _json
                _safe_ctx = _json_safe(card["context"])
                ui.json_editor({"content": {"json": _safe_ctx}}).style(
                    "max-height: 200px;"
                )


# NOTE (Phase 5 Slice 4d): the former ``_render_pending_decisions`` Insights
# "actions" tab renderer was removed.  It was a 4th OperatorDecision surface
# that re-rendered the SAME unified gate cards as the Inbox (via
# ``render_inbox_gate_cards``) — a split-brain the Inbox now owns alone.  The
# pure helper ``_build_pending_decision_view_model`` and the shared
# ``render_decision_card`` above intentionally remain: the Console
# pending-actions mini-pane imports ``render_decision_card``.
