"""T1b/T2/T3 — the OnTheTable page (`/table`), spec UNIFIED-v2 §5.10.c.

Renders the projected inventory (`table_reconciler.project`) as a zoned board:
Services & Accounts · Tools & Capabilities · Files & Data · Keys · Preferences ·
Devices. T2 added curation (search, pin, remove-with-undo, "+ Put on the table",
broken-card repair deep-links); T3 added the guided "Set the table" consult
(§5.10.1) — a SYNCHRONOUS UI flow, not a chat task, so it spends no
harness-request budget. The zoning + summary logic is pure and unit-tested here;
the consult's own logic lives in `runtime/table_consult.py` and is tested there;
the nicegui rendering itself is operator-verifiable.

The page writes only the UI-owned curation sidecars (tombstones/pins/
operator_items, and — via `table_consult.commit` — consulted_items). It never
writes `items.json`, which stays single-writer (the reconciler, DEC-10).

All values crossing into nicegui are plain strings (name/detail/status/kind) — no
functions or non-serializable objects — per the v0.9.45 serialization-crash rule.
"""
from __future__ import annotations

from typing import Any, Dict, List

from nicegui import ui

from systemu.interface.dashboard_state import AppState
from systemu.runtime import table_consult as tc
from systemu.runtime import table_store as ts

# zone label -> the item kinds it holds (ordered for display)
_ZONE_ORDER: List[tuple] = [
    ("Services & Accounts", {"service", "mcp_server"}),
    ("Tools & Capabilities", {"tool"}),
    ("Files & Data", {"data_root"}),
    ("Keys", {"credential_ref"}),
    ("Preferences", {"preference"}),
    ("Devices", {"device"}),
]
_OTHER = "Other"

# status -> Quasar color token (no raw hex; re-theming lives in design tokens).
_STATUS_COLOR = {
    "ready": "positive", "configuring": "warning", "stale": "negative",
    "broken": "negative", "declared": "grey", "suggested": "info",
}

# a compact per-kind summary label
_KIND_SUMMARY = [
    ("mcp_server", "service", "services"),
    ("tool", "tool", "tools"),
    ("data_root", "folder", "folders"),
    ("credential_ref", "key", "keys"),
]


def _kind(it: Any) -> str:
    return getattr(it, "kind", "") or (it.get("kind", "") if isinstance(it, dict) else "")


def zone_of(kind: str) -> str:
    for label, kinds in _ZONE_ORDER:
        if kind in kinds:
            return label
    return _OTHER


def group_into_zones(items: List[Any]) -> Dict[str, List[Any]]:
    """Group TableItems into display zones; drop empty zones, preserve order."""
    zones: Dict[str, List[Any]] = {label: [] for label, _ in _ZONE_ORDER}
    zones[_OTHER] = []
    for it in items:
        zones[zone_of(_kind(it))].append(it)
    return {k: v for k, v in zones.items() if v}


def summarize(items: List[Any]) -> str:
    """A one-line count summary, e.g. '2 services · 12 tools · 3 keys'."""
    counts: Dict[str, int] = {}
    for it in items:
        counts[_kind(it)] = counts.get(_kind(it), 0) + 1
    parts = []
    for kind, singular, plural in _KIND_SUMMARY:
        n = counts.get(kind, 0)
        if n:
            parts.append(f"{n} {singular if n == 1 else plural}")
    return " · ".join(parts) if parts else "nothing yet"


def _project(vault) -> List[Any]:
    if vault is None:
        return []
    try:
        from systemu.runtime import table_reconciler
        return table_reconciler.project(vault)
    except Exception:
        return []


# ── T2a curation: pure helpers (unit-tested; the nicegui actions call these) ──

def filter_items(items: List[Any], query: str) -> List[Any]:
    """Case-insensitive substring filter over name + detail + kind. A blank
    query returns the list unchanged (identity, not a copy)."""
    q = (query or "").strip().lower()
    if not q:
        return items
    out = []
    for it in items:
        hay = " ".join([
            str(getattr(it, "name", "") or ""),
            str(getattr(it, "detail", "") or ""),
            _kind(it),
        ]).lower()
        if q in hay:
            out.append(it)
    return out


def sort_for_display(items: List[Any]) -> List[Any]:
    """Pinned items first, then alphabetical by name (both groups). Stable +
    case-insensitive so the board order is predictable after a pin."""
    return sorted(
        items,
        key=lambda it: (
            0 if getattr(it, "pinned", False) else 1,
            str(getattr(it, "name", "") or "").lower(),
        ),
    )


# kinds that reference a LIVE operational object — removing the table item is a
# view change only; the real credential/server/service keeps existing (§5.10.b
# "explicit still-ACTIVE notice"). Managing the real object is a deep-link (T2b).
_STILL_ACTIVE_KINDS = {"credential_ref", "mcp_server", "service", "tool", "data_root"}
_MANAGE_WHERE = {
    "credential_ref": "Credentials", "mcp_server": "Connections",
    "service": "Connections", "tool": "the Build page", "data_root": "Connections",
}


# broken/stale cards lead with a PRIMARY action that deep-links to the existing
# management surface for the kind (§5.10.c) — navigation only, no new flow, no
# dual-write (the repair happens in the surface the operator lands on).
_REPAIR_ROUTE = {
    "mcp_server": ("/settings", "Reconnect"), "service": ("/settings", "Reconnect"),
    "credential_ref": ("/settings", "Re-add"), "data_root": ("/settings", "Re-grant"),
    "tool": ("/tools", "Fix in Build"),
}


def repair_route(kind: str, status: str) -> tuple:
    """(label, route) for a broken/stale card's primary Fix action, or ('', '')
    when the status is healthy or the kind has no management surface."""
    if status not in ("broken", "stale"):
        return ("", "")
    route_label = _REPAIR_ROUTE.get(kind)
    if not route_label:
        return ("", "")
    route, label = route_label
    return (label, route)


def consult_button_label(vault) -> str:
    """"Set the table" on a cold install, "Resume setting the table" once some
    areas are already answered (§5.10.1). Pure enough to test; defensive because
    it runs while building the header and must never take the page down."""
    try:
        s = tc.ConsultSession()
        remaining = tc.uncovered_areas(vault, s)
    except Exception:
        return "Set the table"
    if not remaining:
        return "Review your table"
    if len(remaining) < len(tc.AREAS):
        return "Resume setting the table"
    return "Set the table"


def removal_notice(kind: str, name: str) -> tuple:
    """(message, still_active) for the post-remove snackbar. For a kind backed
    by a live object the message says plainly that the real thing still exists
    and where to manage it — the table is an intent layer, not the store."""
    if kind in _STILL_ACTIVE_KINDS:
        where = _MANAGE_WHERE.get(kind, "its own page")
        return (
            f"Removed “{name}” from your table. The actual {kind.replace('_', ' ')} "
            f"is still active — manage it in {where}.",
            True,
        )
    return (f"Removed “{name}” from your table.", False)


def _render_card(it: Any, *, on_pin=None, on_remove=None) -> None:
    status = getattr(it, "status", "") or ""
    color = _STATUS_COLOR.get(status, "grey")
    pinned = bool(getattr(it, "pinned", False))
    with ui.card().classes("s-card").style("min-width: 220px; max-width: 340px;"):
        with ui.row().classes("items-center no-wrap w-full justify-between").style("gap: 6px;"):
            with ui.row().classes("items-center no-wrap").style("gap: 6px;"):
                ui.icon("circle", size="xs").props(f"color={color}")
                ui.label(getattr(it, "name", "") or "").classes("text-weight-medium ellipsis")
            # curation actions — pin (★) + remove (×). Handlers close over this item.
            _k, _ref = _kind(it), getattr(it, "ref", {}) or {}
            _nm = getattr(it, "name", "") or ""
            with ui.row().classes("items-center no-wrap").style("gap: 0;"):
                if on_pin is not None:
                    ui.button(
                        icon="star" if pinned else "star_border",
                        on_click=lambda _e=None, k=_k, r=_ref, p=not pinned: on_pin(k, r, p),
                    ).props(
                        f"flat dense round size=sm {'color=amber' if pinned else 'color=grey'}"
                    ).tooltip("Unpin" if pinned else "Pin")
                if on_remove is not None:
                    ui.button(
                        icon="close",
                        on_click=lambda _e=None, k=_k, r=_ref, n=_nm: on_remove(k, r, n),
                    ).props("flat dense round size=sm color=grey").tooltip("Remove from table")
        detail = getattr(it, "detail", "") or ""
        if detail:
            ui.label(detail).classes("s-muted").style("font-size: 12px;")
        with ui.row().classes("items-center").style("gap: 4px;"):
            if status:
                ui.badge(status).props("outline")
            ui.badge(_kind(it) or "item").props("outline color=grey")
            prov = getattr(it, "provenance", "") or ""
            if prov and prov != "migrated":
                ui.badge(prov).props("outline color=blue")
        # broken/stale cards lead with a PRIMARY action deep-linking to the repair
        # surface (§5.10.c) — navigation only (the fix happens where it lands).
        _rlabel, _rroute = repair_route(_kind(it), status)
        if _rlabel:
            ui.link(f"{_rlabel} →", _rroute).classes("s-pill").style(
                "font-size: 12px; margin-top: 4px;")


def build_table_page() -> None:
    state = AppState.get()
    vault = getattr(state, "vault", None)

    # per-page UI state: the live search query, a pending-undo (ref_key, name), and
    # the set of zone labels the operator collapsed (persisted so a board refresh —
    # from a search keystroke / pin / remove / undo-timer — doesn't re-expand them).
    view: Dict[str, Any] = {"query": "", "undo": None, "seq": 0, "collapsed": set()}

    def _clear_undo(seq: int) -> None:
        if view["undo"] is not None and view["seq"] == seq:
            view["undo"] = None
            try:
                _board.refresh()
            except Exception:
                pass

    def _on_remove(kind: str, ref: Dict[str, Any], name: str) -> None:
        try:
            key = ts.ref_key(kind, ref or {})
            ts.add_tombstone(vault, key)
        except Exception:
            ui.notify("Couldn't remove that item.", type="negative")
            return
        msg, still_active = removal_notice(kind, name)
        ui.notify(msg, type="warning" if still_active else "info", multi_line=True)
        view["seq"] += 1
        view["undo"] = (key, name)
        _board.refresh()
        ui.timer(10.0, lambda s=view["seq"]: _clear_undo(s), once=True)

    def _on_undo() -> None:
        if view["undo"]:
            key, _name = view["undo"]
            try:
                ts.remove_tombstone(vault, key)
            except Exception:
                pass
            view["undo"] = None
            _board.refresh()

    def _on_pin(kind: str, ref: Dict[str, Any], pinned: bool) -> None:
        # UI-owned sidecar write (pins.json) — never touches items.json, so pin
        # curation can't race the reconciler (DEC-10 single-writer on items.json).
        try:
            ts.set_pin(vault, ts.ref_key(kind, ref or {}), pinned)
        except Exception:
            pass
        _board.refresh()

    def _on_search(e: Any) -> None:
        view["query"] = getattr(e, "value", "") or ""
        _board.refresh()

    def _on_zone_toggle(label: str, expanded: bool) -> None:
        # record the operator's collapse choice so the next board refresh honors it
        # (no refresh here — the expansion already animated client-side).
        if expanded:
            view["collapsed"].discard(label)
        else:
            view["collapsed"].add(label)

    def _do_add(dlg: Any, kind: str, name: str, detail: str) -> None:
        name = (name or "").strip()
        if not name:
            ui.notify("Give it a name.", type="warning")
            return
        try:
            ts.add_operator_item(vault, ts.make_operator_item(kind, name, (detail or "").strip()))
        except Exception:
            ui.notify("Couldn't add that.", type="negative")
            return
        try:
            dlg.close()
        except Exception:
            pass
        ui.notify(f"Added “{name}” to your table.", type="positive")
        _board.refresh()

    # ── T3 "Set the table" (§5.10.1) — a synchronous, UI-guided flow. NOT a chat
    # task: no ReAct loop, no HarnessRequest, so it spends no harness budget and
    # cannot be force-terminated by a request cap. Ghosts live in `sess` (in
    # memory) until the operator passes the review; only then does `tc.commit`
    # write anything.
    def _open_consult() -> None:
        if not tc.consult_available():
            ui.notify("Connect a model to unlock the guided consult.",
                      type="warning")
            return
        sess = tc.ConsultSession()
        tc.uncovered_areas(vault, sess)            # re-run diff: skip covered areas
        state_: Dict[str, Any] = {"area": tc.next_area(sess), "review": False}

        with ui.dialog().props("persistent") as dlg, \
                ui.card().classes("s-card").style("min-width: 460px; max-width: 620px;"):

            @ui.refreshable
            def _panel() -> None:
                done, total = tc.progress(sess)
                area_id = state_["area"]

                if state_["review"]:
                    # the MANDATORY one-screen review (§5.10.1). Nothing has
                    # landed yet; this is the last look before it does — and the
                    # only fence on a parse that invented something, so every
                    # ghost is shown, renamable and deletable right here.
                    ui.label("Here's your table — anything wrong?").classes("text-h6")
                    ghosts = list(sess.pending)
                    if not ghosts:
                        ui.label("Nothing to add.").classes("s-muted")
                    for idx, ghost in enumerate(ghosts):
                        with ui.row().classes("items-center no-wrap w-full").style("gap: 6px;"):
                            ui.label(tc.area_label_for_kind(ghost.kind)) \
                                .classes("s-muted").style("min-width: 130px; font-size: 12px;")
                            ui.input(value=ghost.name,
                                     on_change=lambda e, i=idx: _rename(i, e.value)) \
                                .props("dense outlined").classes("col")
                            ui.button(icon="close",
                                      on_click=lambda _e=None, i=idx: _drop(i)) \
                                .props("flat dense round size=sm color=grey")
                    with ui.row().classes("w-full justify-end q-mt-sm").style("gap: 8px;"):
                        ui.button("Back", on_click=_back).props("flat color=grey")
                        ui.button(f"Put {len(ghosts)} on my table",
                                  on_click=_commit).props("color=primary")
                    return

                if not area_id:
                    state_["review"] = True
                    _panel.refresh()
                    return

                spec = tc.area(area_id)
                ui.label("Set the table").classes("text-h6")
                ui.label(f"{spec['label']} — {done} of {total}").classes("s-muted")

                # the form is DRIVEN BY the area's `requested_schema` (§5.10.1
                # "reusing the existing requested_schema form path"), so a change
                # to the schema reaches the operator without touching this file.
                schema = tc.area_schema(area_id)
                fields: Dict[str, Any] = {}
                for fname, fspec in (schema.get("properties") or {}).items():
                    label = str(fspec.get("description") or fname)
                    if fname == "items":
                        ui.label(label).classes("q-mt-sm")
                        fields[fname] = ui.textarea() \
                            .props("outlined dense autogrow").classes("w-full")
                    else:
                        fields[fname] = ui.input(label=label) \
                            .props("outlined dense").classes("w-full")

                with ui.row().classes("w-full justify-end q-mt-sm").style("gap: 8px;"):
                    ui.button("Skip", on_click=lambda: _next(area_id, {})) \
                        .props("flat color=grey")
                    ui.button(
                        "Next",
                        on_click=lambda: _next(
                            area_id, {k: w.value for k, w in fields.items()}),
                    ).props("color=primary")
                if sess.pending:
                    ui.separator().classes("q-my-sm")
                    ui.label(f"On your table so far ({len(sess.pending)}) — "
                             "nothing is saved yet").classes("s-muted")
                    for line in tc.review_lines(sess):
                        ui.label(f"· {line}").classes("s-muted ellipsis") \
                            .style("font-size: 12px;")

            def _next(area_id: str, answers: Dict[str, Any]) -> None:
                try:
                    parsed = tc.parse_area_answers(
                        area_id, answers,
                        llm_fn=tc.default_llm_fn(), config=getattr(state, "config", None))
                except Exception:
                    parsed = []
                tc.stage_parsed(sess, area_id, parsed)
                state_["area"] = tc.next_area(sess)
                if not state_["area"]:
                    state_["review"] = True
                _panel.refresh()

            def _rename(index: int, value: str) -> None:
                # no refresh — the operator is typing; re-rendering would steal
                # focus. The ghost's ref/id are re-derived by `edit_pending`.
                tc.edit_pending(sess, index, name=value)

            def _drop(index: int) -> None:
                tc.drop_pending(sess, index)
                _panel.refresh()

            def _back() -> None:
                state_["review"] = False
                state_["area"] = tc.next_area(sess) or tc.AREAS[-1]["id"]
                _panel.refresh()

            def _commit() -> None:
                sess.reviewed = True           # the operator just passed the review
                try:
                    n = tc.commit(vault, sess)
                except Exception:
                    ui.notify("Couldn't save your table.", type="negative")
                    return
                try:
                    dlg.close()
                except Exception:
                    pass
                ui.notify(f"Your table — {n} item(s). "
                          "Click any of them to finish setup.", type="positive")
                _board.refresh()

            _panel()
        dlg.open()

    def _open_add_palette() -> None:
        # "+ Put on the table" (§5.10.c). The declare flows CREATE operator_added
        # items directly (operator-typed = trusted); the deeper setup happens in the
        # existing flows the links route to (no dual-write — §5.10.a).
        with ui.dialog() as dlg, ui.card().classes("s-card").style("min-width: 360px;"):
            ui.label("Put on the table").classes("text-h6")
            ui.label(
                "Declare something you have. systemu will use it, and heal it to the "
                "real thing once you connect it."
            ).classes("s-muted")
            kind_sel = ui.select(
                {"service": "A service or account", "data_root": "A folder",
                 "credential_ref": "A credential (name only — never a secret)"},
                value="service", label="What is it?",
            ).props("outlined dense").classes("w-full q-mt-sm")
            name_in = ui.input(label="Name").props("outlined dense").classes("w-full")
            detail_in = ui.input(label="Note (optional)").props("outlined dense").classes("w-full")
            with ui.row().classes("w-full justify-end q-mt-sm").style("gap: 8px;"):
                ui.button("Cancel", on_click=dlg.close).props("flat color=grey")
                ui.button(
                    "Add",
                    on_click=lambda: _do_add(dlg, kind_sel.value, name_in.value, detail_in.value),
                ).props("color=primary")
            ui.separator().classes("q-my-sm")
            with ui.row().classes("items-center wrap").style("gap: 12px;"):
                ui.label("Or set one up now:").classes("s-muted")
                ui.link("Connect a server / credential →", "/settings").classes("s-muted")
                ui.link("Forge a tool →", "/tools").classes("s-muted")
        dlg.open()

    with ui.row().classes("w-full items-center justify-between q-mb-sm"):
        with ui.column().classes("q-gutter-none"):
            ui.label("On the table").classes("text-h6")
            ui.label(
                "Everything systemu can see it has — services, tools, files, keys. "
                "Pin what matters, remove what's noise."
            ).classes("s-muted")
        with ui.row().classes("items-center no-wrap").style("gap: 8px;"):
            ui.input(placeholder="Search your table…", on_change=_on_search) \
                .props("dense clearable outlined").classes("s-table-search").style("min-width: 200px;")
            # "Set the table" is provider-gated (§5.10.1); with no model the
            # deterministic palette is the only offer, and says why.
            if tc.consult_available():
                # a partly-done table gets the RESUME wording (§5.10.1 "'Resume
                # setting the table' chip returns to the next uncovered area") —
                # the diff-on-rerun that makes it true lives in `uncovered_areas`.
                ui.button(consult_button_label(vault), icon="auto_awesome",
                          on_click=_open_consult).props("dense flat color=primary")
            ui.button("Put on the table", icon="add", on_click=_open_add_palette) \
                .props("dense color=primary")

    @ui.refreshable
    def _board() -> None:
        items = _project(vault)
        ui.label(summarize(items)).classes("s-muted q-mb-xs")

        if view["undo"] is not None:
            with ui.row().classes("items-center s-card q-pa-sm q-mb-sm").style("gap: 10px;"):
                ui.icon("undo", size="sm").props("color=grey")
                ui.label(f"Removed “{view['undo'][1]}”.").classes("s-muted")
                ui.button("Undo", on_click=_on_undo).props("flat dense size=sm color=primary")

        shown = filter_items(items, view["query"])
        zones = group_into_zones(shown)
        if not zones:
            with ui.card().classes("s-card"):
                if items and view["query"].strip():
                    ui.label("No matches.").classes("text-subtitle1")
                    ui.label("Nothing on your table matches that search.").classes("s-muted")
                else:
                    ui.label("Your table is empty.").classes("text-subtitle1")
                    ui.label(
                        "Declare what you have, or connect a service / forge a tool "
                        "and it'll appear here."
                    ).classes("s-muted")
                    # §5.10.c — the empty state leads with the guided consult when
                    # a provider exists, and with the DETERMINISTIC palette when
                    # it does not (saying why, rather than hiding the feature).
                    cta = tc.empty_state_cta()
                    with ui.row().classes("items-center q-mt-sm").style("gap: 8px;"):
                        if cta["primary"] == "set_the_table":
                            ui.button(cta["label"], icon="auto_awesome",
                                      on_click=_open_consult).props("color=primary")
                            ui.button("Put on the table", icon="add",
                                      on_click=_open_add_palette).props("flat color=primary")
                        else:
                            ui.button(cta["label"], icon="add",
                                      on_click=_open_add_palette).props("color=primary")
                    ui.label(cta["note"]).classes("s-muted").style("font-size: 12px;")
            return

        for label, _kinds in list(_ZONE_ORDER) + [(_OTHER, set())]:
            zitems = zones.get(label)
            if not zitems:
                continue
            _exp = ui.expansion(
                f"{label} ({len(zitems)})",
                value=(label not in view["collapsed"]),
            ).classes("w-full q-mt-sm").props("dense")
            _exp.on_value_change(lambda e, L=label: _on_zone_toggle(L, bool(getattr(e, "value", True))))
            with _exp:
                with ui.row().classes("w-full wrap q-mt-xs").style("gap: 8px;"):
                    for it in sort_for_display(zitems):
                        _render_card(it, on_pin=_on_pin, on_remove=_on_remove)

    _board()
