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


def build_elicitation_answer(schema: dict, values: dict) -> str:
    """v0.9.35 (P1) — serialize an elicitation form's per-field values to the
    JSON stored as the decision choice. Only fields declared in the schema's
    properties are emitted (secret fields never enter this dict). The reconciler
    type-coerces these strings via elicitation.param_answers_from_choice."""
    import json
    props = (schema or {}).get("properties") or {}
    return json.dumps({name: (values or {}).get(name) for name in props})


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
        _req_schema = _ctx.get("requested_schema") or {}
        if isinstance(_req_schema, dict) and (_req_schema.get("properties")):
            from systemu.runtime.elicitation import is_secret_field
            _props = _req_schema.get("properties") or {}
            _required = _req_schema.get("required") or []
            _widgets: dict = {}
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
                else:
                    _w = ui.input(_name, value=("" if _default is None else str(_default))
                                  ).style("min-width: 220px;")
                _widgets[_name] = (_ftype, _w)

            def _submit_form(_e=None, did=card["id"], the_queue=queue,
                             schema=_req_schema, widgets=_widgets, required=_required):
                try:
                    vals: dict = {}
                    for _n, (_t, _wg) in widgets.items():
                        _v = getattr(_wg, "value", None)
                        if _t == "boolean":
                            _v = (_v == "Yes") if _v in ("Yes", "No") else None
                        vals[_n] = _v
                    # Client-side required check (secrets excluded from widgets).
                    _missing = [r for r in required
                                if r in widgets and (vals.get(r) in (None, ""))]
                    if _missing:
                        ui.notify("Fill all required fields: " + ", ".join(_missing),
                                  type="warning")
                        return
                    the_queue.resolve(did, choice=build_elicitation_answer(schema, vals))
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

        with ui.row().style("gap: 8px;"):
            for opt in card["options"]:
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
                _safe_ctx = _json.loads(_json.dumps(card["context"], default=str))
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
