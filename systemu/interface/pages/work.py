"""Work page — the workflow-centric list (Phase 5 Slice 2a).

Route: ``/work`` — the Work spine's primary page.  One row per workflow
(workflows are 1:1 with scrolls; ``workflow_id == scroll_id``), each
showing the 5-stage pipeline chips, a status pill, and a link to the
``/workflow/<id>`` detail page.  Rows render from the ``WorkflowTracker``
snapshot (it merges vault + events) — never from raw vault status.

Pure helpers (``work_row_model`` / ``_unlinked_activities``) are
unit-tested without a NiceGUI runtime; ``build_work_page`` composes them
with design-system token classes only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from systemu.runtime.workflow_tracker import STAGES, WorkflowSnapshot


# ─────────────────────────────────────────────────────────────────────────────
#  Pure models
# ─────────────────────────────────────────────────────────────────────────────

# Status → tint class for the row's status pill.  Everything not listed
# renders "ok".  Per the Phase 5 plan: validator_blocked → warn,
# extraction_failed → danger; pending_approval is warn AND carries the
# needs_approval affordance (Slice 2b: Review & Approve → unified gate card).
_DANGER_STATUSES = {"extraction_failed", "failed", "error"}
# W1.2: a readiness-parked task (waiting_on_tools / partial) needs the
# operator — tint warn so it doesn't read as a healthy in-flight row.
_WARN_STATUSES = {"validator_blocked", "pending_approval",
                  "waiting_on_tools", "partial"}


def _status_class(status: str) -> str:
    s = (status or "").lower()
    if s in _DANGER_STATUSES:
        return "danger"
    if s in _WARN_STATUSES:
        return "warn"
    return "ok"


def _chip_link(stage: str, snap: WorkflowSnapshot, reached: bool) -> Optional[str]:
    """Where a reached stage chip navigates (None → passive chip).

    Precedent: pages/workflow_detail.py ``_link_row`` — Scroll → /scrolls,
    Activity → /activities, Shadow → /shadows.  Capture is always passive (it
    has no surface of its own); done points at the workflow detail page.
    """
    if stage == "capture" or not reached:
        return None
    if stage == "scroll":
        return "/scrolls" if snap.scroll_id else None
    if stage == "activity":
        return "/activities" if snap.activity_id else None
    if stage == "execution":
        return "/shadows" if snap.shadow_id else f"/workflow/{snap.workflow_id}"
    if stage == "done":
        return f"/workflow/{snap.workflow_id}"
    return None


def work_row_model(snap: WorkflowSnapshot) -> Dict[str, Any]:
    """Map one WorkflowSnapshot → the /work row dict (pure, testable).

    Reached-chip logic mirrors pages/workflow_detail.py:93-97 — a stage is
    reached when its index ≤ the current stage's index OR the timeline has
    a timestamp for it.  ``snap.stage`` may be "failed" (terminal, not in
    STAGES) — rank degrades to -1 so only timeline entries count.
    """
    current_rank = STAGES.index(snap.stage) if snap.stage in STAGES else -1
    chips: List[Dict[str, Any]] = []
    for idx, stage in enumerate(STAGES):
        reached = (
            stage == "capture"                      # capture is always reached
            or idx <= current_rank
            or bool(snap.timeline.get(stage))
        )
        chips.append({
            "stage": stage,
            "reached": reached,
            "link": _chip_link(stage, snap, reached),
        })
    return {
        "workflow_id": snap.workflow_id,
        "title": snap.title,
        "status": snap.status,
        "status_class": _status_class(snap.status),
        "updated_at": snap.updated_at,
        "chips": chips,
        "detail_link": f"/workflow/{snap.workflow_id}",
        "needs_approval": snap.status == "pending_approval",
    }


def _unlinked_activities(scrolls, activities) -> List[Dict[str, Any]]:
    """Activities whose ``scroll_id`` is not a known scroll id (pure).

    THIN defensive fallback: coverage is 100% by construction today
    (Activity.scroll_id is required and every scroll seeds a workflow) —
    this only catches vault drift (a scroll deleted out from under its
    activities).  Rendered as an "Unlinked items" section ONLY when
    non-empty.
    """
    scroll_ids = {s.get("id") for s in (scrolls or []) if s.get("id")}
    return [a for a in (activities or []) if a.get("scroll_id") not in scroll_ids]


def _filter_rows(rows: List[Dict[str, Any]], query, status) -> List[Dict[str, Any]]:
    """Filter row dicts by free-text query (title or workflow_id, case-
    insensitive) and exact status.  ``""``/``"all"``/None pass everything."""
    q = (query or "").strip().lower()
    s = status or ""
    out: List[Dict[str, Any]] = []
    for r in rows:
        if q and q not in (r.get("title") or "").lower() \
                and q not in (r.get("workflow_id") or "").lower():
            continue
        if s and s != "all" and r.get("status") != s:
            continue
        out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Page
# ─────────────────────────────────────────────────────────────────────────────

# status_class → s-pill tint token (the only mapping the renderer needs).
_PILL_CLASS = {"ok": "s-pill--success", "warn": "s-pill--warn", "danger": "s-pill--danger"}

# W13.4: statuses after which "Run again" makes sense — the workflow has
# actually run to a terminal state (success OR failure: re-running a failed
# run is the most common operator wish).
_RERUN_STATUSES = {"completed", "success", "done", "failed", "failure",
                   "partial", "cancelled"}


def can_rerun(row: Dict[str, Any]) -> bool:
    """W13.4: the repeatability payoff, made visible — offered once the
    workflow reached a terminal state and isn't waiting on approval."""
    if row.get("needs_approval"):
        return False
    return str(row.get("status", "")).lower() in _RERUN_STATUSES


def _resubmit_activity(act, *, label: str) -> str:
    """Shared core: validate the activity has an assigned shadow, then resubmit a
    fresh execution (Supervisor dedups in-flight duplicates). Raises with a
    plain-language reason when it can't."""
    if act is None:
        raise ValueError("this workflow has no runnable activity yet")
    shadow_id = act.get("assigned_shadow_id")
    if not shadow_id:
        raise ValueError("no specialist assigned yet — approve the workflow first")
    from systemu.runtime.supervisor import Supervisor
    Supervisor.get().submit(act["id"], shadow_id, reason="operator_rerun",
                            scroll_id=act.get("scroll_id"))
    return f"Re-running '{act.get('name') or label}' — watch Live."


def rerun_workflow(scroll_id: str) -> str:
    """Resubmit the workflow's activity (looked up by scroll_id) to its assigned
    shadow. Returns the toast message; raises with a plain reason when it can't."""
    from systemu.interface.dashboard_state import AppState
    acts = AppState.get().vault.load_index("activities") or []
    return _resubmit_activity(
        next((a for a in acts if a.get("scroll_id") == scroll_id), None), label=scroll_id)


def rerun_workflow_by_activity(activity_id: str) -> str:
    """Resubmit by ACTIVITY id — used by the live-events Re-trigger button, whose
    event context carries activity_id (not scroll_id). DRY sibling of
    rerun_workflow."""
    from systemu.interface.dashboard_state import AppState
    acts = AppState.get().vault.load_index("activities") or []
    return _resubmit_activity(
        next((a for a in acts if a.get("id") == activity_id), None), label=activity_id)

def _short_ts(iso: str) -> str:
    """ISO timestamp → human-readable (seconds precision)."""
    return (iso or "")[:19].replace("T", " ")


def _load_rows() -> List[Dict[str, Any]]:
    """Tracker → sorted row models.  Defensive: any failure → empty list."""
    try:
        from systemu.runtime.workflow_tracker import WorkflowTracker
        tracker = WorkflowTracker.get()
        tracker.refresh_from_vault()
        rows = [work_row_model(s) for s in tracker.list_all()]
        rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return rows
    except Exception:
        return []


def _load_unlinked() -> List[Dict[str, Any]]:
    """Vault → unlinked activities.  Defensive: any failure → empty list."""
    try:
        from systemu.interface.dashboard_state import AppState
        vault = AppState.get().vault
        return _unlinked_activities(
            vault.load_index("scrolls"), vault.load_index("activities"),
        )
    except Exception:
        return []


def build_work_page() -> None:
    """Render the /work list: search + status filter over tracker rows,
    5-stage chips per row, and the defensive "Unlinked items" section.
    Token classes / plain-string styles only (lint stays at 0 new)."""
    from nicegui import ui
    from systemu.interface.ui_helpers import safe_timer

    # Filter state survives timer refreshes (the refreshable takes no args).
    filt = {"query": "", "status": "all"}

    # ── Header + filter bar ────────────────────────────────────────────
    with ui.row().classes("w-full items-center justify-between q-mb-md"):
        with ui.column().style("gap: 2px;"):
            from systemu.interface.design.glossary import lore_sublabel
            ui.label(lore_sublabel("work")).classes("s-muted")

    initial_rows = _load_rows()
    statuses = sorted({r["status"] for r in initial_rows if r.get("status")})

    def _on_search(e) -> None:
        filt["query"] = e.value if isinstance(e.value, str) else ""
        _rows_view.refresh()

    def _on_status(e) -> None:
        filt["status"] = e.value or "all"
        _rows_view.refresh()

    with ui.row().classes("w-full items-center q-gutter-sm q-mb-md"):
        ui.input(
            placeholder="Search workflows...", on_change=_on_search,
        ).classes("s-input s-search")
        ui.select(
            ["all"] + statuses, value="all", on_change=_on_status,
        ).classes("s-input")

    # ── Rows ───────────────────────────────────────────────────────────
    @ui.refreshable
    def _rows_view() -> None:
        rows = _filter_rows(_load_rows(), filt["query"], filt["status"])
        if not rows:
            with ui.row().classes("q-pa-md items-center").style("gap: 6px;"):
                ui.label("No workflows yet —").classes("s-muted")
                ui.link("submit a task in Chat", "/chat").classes("s-muted")
                ui.label("or hit ＋New → Record session.").classes("s-muted")
        for row in rows:
            _render_row(row, on_refresh=_rows_view.refresh)

        unlinked = _load_unlinked()
        if unlinked:
            ui.label("Unlinked items").classes("s-section-head q-mt-md")
            for act in unlinked:
                _render_unlinked_row(act)

    _rows_view()

    # W12 (ship-blocker, audit-caught live): change-gated liveness. The
    # unconditional 2s repaint destroyed and rebuilt every row button, so a
    # click arriving for the just-destroyed element was silently dropped —
    # "Review & Approve did nothing" (the W11.1 expansion bug's click-eating
    # sibling). Repaint only when the rows actually changed; user-driven
    # filter changes still refresh directly.
    import json as _json

    from systemu.interface.ui_helpers import gated_refresh

    def _fingerprint() -> str:
        rows = _filter_rows(_load_rows(), filt["query"], filt["status"])
        # updated_at is stamped on every tracker poll — including it would
        # change the fingerprint every tick and defeat the gate entirely.
        stable = [{k: v for k, v in r.items() if k != "updated_at"}
                  for r in rows]
        return _json.dumps([stable, _load_unlinked()], sort_keys=True,
                           default=str)

    safe_timer(2.0, gated_refresh(_fingerprint, _rows_view.refresh))


def _render_row(row: Dict[str, Any], on_refresh=None) -> None:
    """One workflow row: title link, 5 stage chips, status pill, updated-at.
    ``on_refresh`` re-renders the rows after a gate resolution (Slice 2b)."""
    from nicegui import ui
    from systemu.interface.design.primitives import card

    with card(classes="w-full q-mb-sm"):
        with ui.row().classes("w-full items-center q-gutter-sm"):
            # Title → workflow detail
            ui.link(row["title"], row["detail_link"]) \
                .classes("s-cell s-cell--bold col-grow") \
                .style("text-decoration: none;")

            # 5 stage chips (filled = reached, muted = unreached; clickable
            # when the stage has a surface to open)
            with ui.row().classes("items-center q-gutter-xs"):
                for chip in row["chips"]:
                    _render_stage_chip(chip)

            # Status pill (tracker status, tinted per status_class)
            pill = _PILL_CLASS.get(row["status_class"], "s-pill--muted")
            ui.label(row["status"]).classes(f"s-pill {pill}")

            # Slice 2b: inspect-before-approve — open the unified gate card
            # (ensure_scroll_gate enqueues on demand; workflow_id == scroll_id).
            if row["needs_approval"]:
                from systemu.interface.scroll_gate import open_scroll_review_dialog
                ui.button(
                    "Review & Approve",
                    on_click=lambda _, i=row["workflow_id"]:
                        open_scroll_review_dialog(i, on_resolved=on_refresh),
                ).classes("s-btn s-btn--primary")
            elif can_rerun(row):
                # W13.4: one click re-runs a learned workflow (W7.1 pattern:
                # submit off-loop, re-enter the captured client after).
                async def _on_rerun(_=None, sid=row["workflow_id"]):
                    import asyncio
                    try:
                        client = ui.context.client
                    except Exception:
                        client = None
                    try:
                        msg = await asyncio.to_thread(rerun_workflow, sid)
                        typ = "positive"
                    except Exception as exc:
                        msg, typ = f"Could not re-run: {exc}", "negative"
                    if client is not None:
                        try:
                            with client:
                                ui.notify(msg, type=typ)
                        except Exception:
                            pass

                ui.button("Run again", on_click=_on_rerun).classes(
                    "s-btn s-btn--ghost")

            ui.label(_short_ts(row["updated_at"])).classes("s-mono")


def _render_stage_chip(chip: Dict[str, Any]) -> None:
    from nicegui import ui

    text = chip["stage"]
    cls = "s-pill " + ("s-pill--accent" if chip["reached"] else "s-pill--muted")
    if chip["link"]:
        ui.link(text, chip["link"]).classes(cls) \
            .style("text-decoration: none; cursor: pointer;")
    else:
        ui.label(text).classes(cls)


def _render_unlinked_row(act: Dict[str, Any]) -> None:
    """Same row chrome as a workflow row, with a greyed "missing scroll"
    chip — only ever rendered when the defensive fallback found drift."""
    from nicegui import ui
    from systemu.interface.design.primitives import card

    aid = act.get("id", "?")
    with card(classes="w-full q-mb-sm"):
        with ui.row().classes("w-full items-center q-gutter-sm"):
            ui.link(act.get("name") or aid, "/activities") \
                .classes("s-cell s-cell--bold col-grow") \
                .style("text-decoration: none;")
            ui.label("missing scroll").classes("s-pill s-pill--muted")
            ui.label(act.get("status") or "unknown").classes("s-pill s-pill--muted")
