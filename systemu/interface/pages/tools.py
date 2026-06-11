"""NiceGUI Dashboard — Tools Registry page.

Three mandatory human gates before a tool can be used by a Shadow:
  Gate 1 — Spec Review    : user reads and edits the extracted tool spec JSON
  Gate 2 — Code Review    : user reads LLM-generated code + risk pattern report
  Gate 3 — Explicit Enable: user flips the toggle ON in this registry

Status visual guide:
  PROPOSED              → grey badge, "Review & Forge" button
  FORGED  + disabled    → amber badge, toggle OFF
  FORGED  + enabled     → green badge, toggle ON
  DEPLOYED / other      → inherit existing badge color
"""

from __future__ import annotations

import asyncio
import json as _json
import logging

from nicegui import ui

from systemu.interface.dashboard_state import AppState, THEME, status_badge_html

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  v0.7.4 Pattern 2 — per-row action helpers (pure-data so unit-testable)
# ─────────────────────────────────────────────────────────────────────────────

# v0.7.4 Pattern 2 policy now lives in the shared renderer module
# (entity_rows) so the Tools page and any other lister share ONE definition.
# Re-exported here so existing imports (`from ...pages.tools import
# _row_actions_for`) and tests keep resolving.
from systemu.interface.components.entity_rows import _row_actions_for  # noqa: E402,F401


def _dispatch_dryrun(tool_id: str) -> None:
    """Dashboard [Dry-Run] button → stream `tools dry-run <id>` through dispatch().

    Phase 2 P2-reduction: the hand-built ``[sys.executable, '-m', 'sharing_on',
    ...]`` argv is gone — the dashboard caller now goes through the single
    ``dispatch()`` contract (stream=True spawns via JobManager and returns a
    CommandResult whose ``stream_ref`` is the Job.id the rail can follow).
    """
    from systemu.interface.command.dispatch import dispatch
    state = AppState.get()
    result = dispatch(
        "tools dry-run", [tool_id],
        cwd=state.project_root, stream=True,
        dedup_key=f"dryrun:{tool_id}",
    )
    if result.status.value == "ok":
        ui.notify(
            f"Dry-run dispatched for {tool_id[:8]} (stream {result.stream_ref})",
            type="positive",
        )
    else:
        ui.notify(f"Failed to dispatch dry-run: {result.summary}", type="negative")


def _dispatch_enable(tool_id: str) -> None:
    """Dashboard [Enable] button → stream `tools enable <id>` through dispatch().

    Same single-entry contract as the dry-run caller: ``dispatch(stream=True)``
    spawns the verb via JobManager and returns a CommandResult whose
    ``stream_ref`` (= Job.id) the right-rail follows.
    """
    from systemu.interface.command.dispatch import dispatch
    state = AppState.get()
    result = dispatch(
        "tools enable", [tool_id],
        cwd=state.project_root, stream=True,
        dedup_key=f"enable:{tool_id}",
    )
    if result.status.value == "ok":
        ui.notify(
            f"Enable dispatched for {tool_id[:8]} (stream {result.stream_ref})",
            type="positive",
        )
    else:
        ui.notify(f"Failed to dispatch enable: {result.summary}", type="negative")


# W2.1: the dangerous-pattern catalogue moved to systemu.runtime.code_risk
# (AST-based scan; the old substring matching was trivially bypassed by
# getattr/string concatenation). _detect_risks below delegates to it.


# ─────────────────────────────────────────────────────────────────────────────
#  Page builder
# ─────────────────────────────────────────────────────────────────────────────

def _render_unbaked_banner() -> None:
    """v0.6.8-e: surface runtime-approved deps that haven't been baked into
    the docker image yet — they live in pip on the running container but
    will vanish on the next ``docker compose up`` unless ``docker compose
    build`` is run first.  Best-effort: any failure (no DB URL, no table)
    silently no-ops so the page still renders in fresh / local-mode
    installs."""
    try:
        from systemu.runtime.dep_approvals import list_unbaked_approvals
        unbaked = list_unbaked_approvals()
    except Exception:
        return
    if not unbaked:
        return
    names = ", ".join(a.package_name for a in unbaked)
    with ui.row().classes("s-banner s-banner--info w-full q-mb-md"):
        ui.icon("info")
        ui.label(
            f"{len(unbaked)} runtime-approved dep(s) not yet baked into image "
            f"({names}). Next `docker compose build` picks them up."
        )


def _filter_tools(tools, query, status):
    """Pure filter for the Tools registry toolbar (board 5a: search + status).

    ``query`` matches name/description case-insensitively; ``status`` is an
    exact (case-insensitive) match, with ``""``/``"all"`` meaning no status
    filter.  Tolerant of dicts missing the ``status``/``description`` keys.
    """
    q = (query or "").strip().lower()
    st = (status or "all").strip().lower()
    out = []
    for t in tools:
        if st not in ("", "all") and (t.get("status") or "").lower() != st:
            continue
        if q and q not in f"{t.get('name', '')} {t.get('description', '')}".lower():
            continue
        out.append(t)
    return out


def build_tools_page(forge_tool_id: str | None = None) -> None:
    state = AppState.get()
    vault = state.vault

    # Deep-link: /tools?forge=<id> auto-opens the spec/code review dialog after
    # the page has rendered (ui.timer defers past the slot-stack build).  This
    # is scheduled before the no-tools early return so the deep-link still
    # resolves (the dialog notifies + no-ops on an unknown/missing id).
    if forge_tool_id:
        def _open_forge(tid: str = forge_tool_id) -> None:
            try:
                _show_spec_review_dialog(tid)
            except Exception:
                ui.notify("Could not open the tool review dialog.", type="negative")
        ui.timer(0.1, _open_forge, once=True)

    _render_unbaked_banner()

    # board 5a: live ⌕ search + status ▾ filter over the registry.
    _all_tools = vault.load_index("tools")
    _statuses = sorted({(t.get("status") or "") for t in _all_tools if t.get("status")})
    _filt = {"query": "", "status": "all"}

    def _on_tool_search(e) -> None:
        _filt["query"] = e.value if isinstance(e.value, str) else ""
        _tools_table.refresh()

    def _on_tool_status(e) -> None:
        _filt["status"] = e.value or "all"
        _tools_table.refresh()

    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        ui.label("Tools").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )
        with ui.row().classes("items-center q-gutter-sm"):
            if _all_tools:
                ui.input(placeholder="Search tools...", on_change=_on_tool_search) \
                    .classes("s-input s-search")
                ui.select(["all"] + _statuses, value="all", on_change=_on_tool_status) \
                    .classes("s-input")
            ui.button("+ New Tool", on_click=_show_forge_dialog).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px;"
            )

    # Tool-dependency approval card (v0.3.4).  Appears above the registry
    # table so the operator sees pending pip-install requests before
    # digging through the tool list.
    with ui.column().classes("w-full").style("margin-bottom: 24px;"):
        ui.label("Tool Dependencies").style(
            f"font-size: 14px; color: {THEME['text_muted']}; font-weight: 700; "
            f"letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 8px;"
        )
        from systemu.interface.components.pending_deps import build_pending_deps
        build_pending_deps(compact=False)

    # Recalibration approval cards (v0.5.0-e).  Pulled from EventBus
    # buffer and rendered with action buttons; only shows when there
    # are recalibrations awaiting decision.
    _build_recalibration_cards(vault)

    if not _all_tools:
        ui.label("No tools yet. Process a scroll to auto-discover tools.").style(
            f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
        )
        return

    # v0.9 Phase-5 3a: ONE canonical renderer per tool (entity_rows). The page
    # only owns the table chrome + headers now; each <tr> (cells + actions +
    # the Gate-3 toggle wiring) comes from render_tool_row so the registry,
    # workshop and any future lister paint identically.
    from systemu.interface.components.entity_rows import render_tool_row

    @ui.refreshable
    def _tools_table() -> None:
        rows = _filter_tools(vault.load_index("tools"), _filt["query"], _filt["status"])
        with ui.element("table").style(
            f"width: 100%; border-collapse: collapse; background: {THEME['surface']}; "
            f"border: 1px solid {THEME['border']}; border-radius: 12px; overflow: hidden;"
        ):
            with ui.element("thead"):
                with ui.element("tr"):
                    for col in ["Name", "Type", "Status", "Enabled", "Dry-run",
                                "Success", "Deps", "Description", "Actions"]:
                        with ui.element("th").style(
                            f"background: {THEME['surface2']}; color: {THEME['text_muted']}; "
                            f"font-size: 11px; font-weight: 600; text-transform: uppercase; "
                            f"letter-spacing: 0.08em; padding: 10px 16px; text-align: left; "
                            f"border-bottom: 1px solid {THEME['border']};"
                        ):
                            ui.label(col)

            with ui.element("tbody"):
                if not rows:
                    with ui.element("tr"):
                        with ui.element("td").props('colspan="9"').style("padding: 18px;"):
                            ui.label("No tools match the current filter.").style(
                                f"color: {THEME['text_muted']}; font-style: italic;"
                            )
                for t in rows:
                    render_tool_row(t, vault, editable=True)

    _tools_table()


# ─────────────────────────────────────────────────────────────────────────────
#  Gate 1 → Gate 2 spec/code review dialog
# ─────────────────────────────────────────────────────────────────────────────

def _build_recalibration_cards(vault) -> None:
    """v0.5.0-e: surface pending RECALIBRATE_TOOL approvals on the Tools page.

    Scans the EventBus ring buffer for unresolved
    ``tool-recalibrate:*`` approval cards.  Renders each as an actionable
    panel with four operator choices:

      * **Enable & Resume** — enable the recalibrated tool + re-queue the
        activity (auto-maps new tool to shadow on fork).
      * **Override → Bump** — operator wants in-place bump even though
        the supervisor proposed fork.
      * **Override → Fork** — operator wants a fresh tool even though
        the supervisor proposed bump.
      * **Reject** — discard the recalibration; the activity stays dead.

    For v0.5.0-e, the **Enable & Resume** path is fully wired.  Override
    paths emit an info notice and leave the recalibration in the audit
    log for v0.5.1+ deeper handling.
    """
    try:
        from systemu.interface.event_bus import EventBus
        buf = EventBus.get().get_buffer()
    except Exception:
        return

    # Pick the most recent unresolved tool-recalibrate card per dedup_key.
    pending: dict = {}
    resolved_keys: set = set()
    for ev in buf:
        cat = ev.get("category")
        ctx = ev.get("context") or {}
        key = ctx.get("dedup_key", "")
        if not key.startswith("tool-recalibrate:"):
            continue
        if cat == "approval_dismissed":
            resolved_keys.add(key)
        elif cat == "approval":
            pending[key] = ev

    active = [(k, ev) for k, ev in pending.items() if k not in resolved_keys]
    if not active:
        return

    ui.label("Pending Tool Recalibrations").style(
        f"font-size: 14px; color: {THEME['warning']}; font-weight: 700; "
        f"letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 8px;"
    )
    for _key, ev in active:
        _render_recalibration_card(ev, vault)


def _render_recalibration_card(ev: dict, vault) -> None:
    ctx  = ev.get("context") or {}
    rec  = ctx.get("recalibration") or {}
    mode = rec.get("mode", "?")
    fallback = rec.get("forced_fallback", False)
    with ui.column().classes("w-full").style(
        f"background: color-mix(in srgb, {THEME['warning']} 8%, {THEME['surface']}); "
        f"border: 1px solid color-mix(in srgb, {THEME['warning']} 40%, transparent); "
        f"border-radius: 12px; padding: 16px; gap: 10px; margin-bottom: 14px;"
    ):
        ui.label(f"Mode: {mode}" + (" (fallback from bump)" if fallback else "")).style(
            f"font-size: 13px; font-weight: 700; color: {THEME['warning']};"
        )
        ui.label(ctx.get("approval_message") or "").style(
            f"font-size: 12px; color: {THEME['text']}; white-space: pre-wrap;"
        )
        # v0.5.1-b: spec diff (collapsible).  Shows what changed in the
        # tool spec so the operator can audit before approving.
        spec_diff = rec.get("spec_diff") or []
        if spec_diff:
            with ui.expansion(f"View spec diff ({len(spec_diff)} field(s) changed)").classes("w-full").style(
                f"background: color-mix(in srgb, {THEME['border']} 20%, transparent); "
                f"border-radius: 8px; margin-top: 4px;"
            ):
                for entry in spec_diff:
                    ui.label(entry.get("field", "")).style(
                        f"font-family: monospace; font-size: 11px; "
                        f"color: {THEME['text_muted']}; font-weight: 700; "
                        f"text-transform: uppercase; letter-spacing: 0.06em; "
                        f"margin-top: 8px;"
                    )
                    with ui.row().style("gap: 8px; align-items: flex-start; width: 100%;"):
                        with ui.column().style("flex: 1; gap: 2px;"):
                            ui.label("OLD").style(
                                f"font-size: 10px; color: {THEME['danger']}; font-weight: 700;"
                            )
                            ui.label(entry.get("old", "(none)")).style(
                                f"font-family: monospace; font-size: 11px; "
                                f"color: {THEME['text']}; white-space: pre-wrap; "
                                f"background: color-mix(in srgb, {THEME['danger']} 8%, transparent); "
                                f"padding: 4px; border-radius: 4px;"
                            )
                        with ui.column().style("flex: 1; gap: 2px;"):
                            ui.label("NEW").style(
                                f"font-size: 10px; color: {THEME['success']}; font-weight: 700;"
                            )
                            ui.label(entry.get("new", "(none)")).style(
                                f"font-family: monospace; font-size: 11px; "
                                f"color: {THEME['text']}; white-space: pre-wrap; "
                                f"background: color-mix(in srgb, {THEME['success']} 8%, transparent); "
                                f"padding: 4px; border-radius: 4px;"
                            )
        with ui.row().style("gap: 8px; flex-wrap: wrap; margin-top: 4px;"):
            ui.button(
                "✓ Enable & Resume",
                on_click=lambda r=rec, c=ctx: _on_enable_and_resume(r, c, vault),
            ).style(
                f"background: {THEME['success']}; color: white; border-radius: 6px; "
                f"font-size: 12px; font-weight: 600; padding: 6px 12px;"
            )
            # v0.5.1-a: operator override actions.  When the supervisor
            # recommended one mode, operator can force the other.  Hides
            # the override that matches the supervisor's pick (no point
            # re-running the same recalibration).
            current_mode = rec.get("mode") or "bump_version"
            if current_mode != "bump_version":
                ui.button(
                    "Override → Bump",
                    on_click=lambda r=rec, c=ctx: _on_override_recalibration(
                        r, c, vault, forced_mode="bump_version",
                    ),
                ).style(
                    f"background: {THEME['warning']}; color: white; border-radius: 6px; "
                    f"font-size: 12px; font-weight: 600; padding: 6px 12px;"
                )
            if current_mode != "fork_new_tool":
                ui.button(
                    "Override → Fork",
                    on_click=lambda r=rec, c=ctx: _on_override_recalibration(
                        r, c, vault, forced_mode="fork_new_tool",
                    ),
                ).style(
                    f"background: {THEME['warning']}; color: white; border-radius: 6px; "
                    f"font-size: 12px; font-weight: 600; padding: 6px 12px;"
                )
            ui.button(
                "Reject",
                on_click=lambda c=ctx: _on_reject_recalibration(c),
            ).style(
                f"background: transparent; color: {THEME['danger']}; "
                f"border: 1px solid {THEME['danger']}; border-radius: 6px; "
                f"font-size: 12px; padding: 6px 12px;"
            )


def _on_enable_and_resume(rec: dict, ctx: dict, vault) -> None:
    """v0.5.0-e — enable the new tool + ask the Supervisor to resume."""
    new_tool_id      = rec.get("new_tool_id") or rec.get("original_tool_id")
    original_tool_id = rec.get("original_tool_id")
    mode             = rec.get("mode") or "bump_version"
    if not new_tool_id:
        ui.notify("No new tool id on the card — cannot resume.", type="negative")
        return
    try:
        # Mark the new tool enabled.
        t = vault.get_tool(new_tool_id)
        t.enabled = True
        # If dry-run was passed, leave status alone; else mark passed
        # (operator override of skipped status).
        if (getattr(t, "dry_run_status", None) or "not_run") not in ("passed", "skipped"):
            t.dry_run_status = "skipped"
        vault.save_tool(t)
    except Exception as exc:
        ui.notify(f"Could not enable tool: {exc}", type="negative")
        return

    # Re-queue activity via supervisor.
    try:
        from systemu.runtime.supervisor import Supervisor
        sup = Supervisor.get()
        sub_id = sup.resume_after_recalibration(
            execution_id=ctx.get("execution_id", ""),
            original_tool_id=original_tool_id,
            new_tool_id=new_tool_id,
            mode=mode,
            original_shadow_id=ctx.get("shadow_id", ""),
            scroll_id=ctx.get("scroll_id"),
        )
    except Exception as exc:
        ui.notify(f"Supervisor resume failed: {exc}", type="negative")
        return

    # Dismiss the card via the v0.3.6 dismissed-event pattern.
    try:
        from systemu.interface.event_bus import EventBus
        from datetime import datetime, timezone
        EventBus.get().publish({
            "ts":       datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "level":    "SUCCESS",
            "category": "approval_dismissed",
            "message":  f"Recalibration approved → submission {sub_id}",
            "context": {
                "dedup_key": ctx.get("dedup_key"),
                "outcome":   "approved",
            },
        })
    except Exception as exc:
        # EventBus publish is best-effort, but log the swallowed approval event
        # so a missing audit-trail entry is traceable.
        logger.warning("Failed to publish recalibration-approved event: %s", exc)
    ui.notify(
        f"Tool '{rec.get('new_tool_name', new_tool_id)}' enabled. "
        f"Activity re-queued ({sub_id}).",
        type="positive", multi_line=True,
    )


def _on_override_recalibration(
    rec: dict,
    ctx: dict,
    vault,
    *,
    forced_mode: str,
) -> None:
    """v0.5.1-a — operator forces a different recalibration mode.

    Re-runs the recalibration pipeline with the operator's chosen mode
    instead of the supervisor's recommendation.  The new run produces a
    fresh approval card; the operator then approves that one normally.

    Used when the supervisor proposed bump and operator wants fork (e.g.
    "I know our other shadows depend on the current behaviour"), or vice
    versa ("I trust the new code; I don't need the audit-isolation a
    fork gives me").
    """
    original_tool_id = rec.get("original_tool_id")
    shadow_id        = ctx.get("shadow_id")
    execution_id     = ctx.get("execution_id", "")
    scroll_id        = ctx.get("scroll_id")
    if not original_tool_id or not shadow_id:
        ui.notify("Missing context for override — cannot recalibrate.", type="negative")
        return
    try:
        from sharing_on.config import Config
        from systemu.pipelines.tool_inadequacy_diagnosis import InadequacyDiagnosis
        from systemu.pipelines.tool_recalibrator import (
            publish_recalibration_card, recalibrate_tool,
        )
        config = Config.from_env()

        original_tool = vault.get_tool(original_tool_id)
        shadow = vault.get_shadow(shadow_id)

        # Build a synthetic diagnosis carrying the operator's forced mode.
        # We preserve the original supervisor's rationale so the audit trail
        # shows both: supervisor recommended X, operator overrode to Y.
        original_rationale = rec.get("rationale", "") or ""
        diagnosis = InadequacyDiagnosis(
            is_inadequate=True,
            recalibration_mode=forced_mode,
            rationale=(
                f"OPERATOR OVERRIDE: forced {forced_mode} "
                f"(supervisor recommended {rec.get('mode', 'unknown')}). "
                f"Original rationale: {original_rationale[:300]}"
            ),
            spec_diff_summary=rec.get("spec_diff_summary", "") or "",
            new_tool_name_suggestion=rec.get("new_tool_name", ""),
            affected_shadows=[],
            confidence="high",
        )
        result = recalibrate_tool(
            tool=original_tool, shadow=shadow, diagnosis=diagnosis,
            failure_context=original_rationale[:400],
            config=config, vault=vault, execution_id=execution_id,
        )
        # Dismiss the original card; publish_recalibration_card will create a
        # fresh card for the operator's overridden recalibration.
        try:
            from datetime import datetime, timezone
            from systemu.interface.event_bus import EventBus
            EventBus.get().publish({
                "ts":       datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                "level":    "INFO",
                "category": "approval_dismissed",
                "message":  f"Original recalibration dismissed (operator overrode → {forced_mode})",
                "context": {
                    "dedup_key": ctx.get("dedup_key"),
                    "outcome":   "overridden",
                },
            })
        except Exception as exc:
            logger.debug("Failed to publish recalibration-overridden event: %s", exc)
        publish_recalibration_card(
            result=result, shadow_id=shadow_id,
            execution_id=execution_id, scroll_id=scroll_id,
        )
        ui.notify(
            f"Override re-ran as {forced_mode} ({result.dry_run_status}). "
            "Review the new card to enable.",
            type="positive", multi_line=True,
        )
    except Exception as exc:
        ui.notify(f"Override failed: {exc}", type="negative")


def _on_reject_recalibration(ctx: dict) -> None:
    """Reject the recalibration; recorded in rejection_store for v0.4.1-c
    feedback learning so the supervisor backs off on similar signatures.
    """
    try:
        from systemu.runtime.rejection_store import get_rejection_store
        sig = (ctx.get("dedup_key") or "")
        get_rejection_store().record_rejection(
            sig, dedup_key=ctx.get("dedup_key"), action="RECALIBRATE_TOOL",
            reason="operator_rejected",
        )
    except Exception as exc:
        # Recording the rejection feeds supervisor backoff learning — surface
        # a failure so a silently-lost rejection is traceable.
        logger.warning("Failed to record recalibration rejection: %s", exc)
    try:
        from systemu.interface.event_bus import EventBus
        from datetime import datetime, timezone
        EventBus.get().publish({
            "ts":       datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "level":    "WARNING",
            "category": "approval_dismissed",
            "message":  "Recalibration rejected by operator",
            "context": {
                "dedup_key": ctx.get("dedup_key"),
                "outcome":   "rejected",
            },
        })
    except Exception as exc:
        logger.debug("Failed to publish recalibration-rejected event: %s", exc)
    ui.notify("Recalibration rejected. Supervisor will avoid similar proposals.",
               type="warning")


def _approve_install_and_enable(tool_id, vault, config) -> None:
    """v0.8.13 Fix 6c: after spec+code review sign-off, install declared deps,
    enable the tool, and heal waiting activities. Operator has reviewed the code."""
    from systemu.runtime.dep_approvals import approve_and_install
    from systemu.pipelines.tool_service import enable_tool, heal_activities_for_tool
    try:
        tool = vault.get_tool(tool_id)
    except KeyError:
        return
    for pkg in (tool.dependencies or []):
        try:
            approve_and_install(tool_id=tool_id, package=pkg, source="dashboard-review")
        except Exception:
            logger.warning("[Tools] dep install failed for %s/%s", tool_id, pkg, exc_info=True)
    enable_tool(tool_id, vault)
    heal_activities_for_tool(tool_id, config, vault)


def _resolve_forge_gate_silently(tool_id: str, vault, *, choice: str) -> None:
    """Best-effort: resolve the matching ``forge:<tool_id>`` Inbox gate row so it
    can no longer be Approved from the Inbox after this dialog has already taken
    a terminal decision (forged via Gate-2 review, or rejected).

    This is the double-forge guard for the rich review path: the dialog uses the
    interactive ``preview_tool_code``→``save_approved_code`` flow (human reads
    the code before sign-off), NOT the gate's one-shot ``forge_tool_from_spec``.
    Without this, a lingering ``forge:`` gate could later be resolved via the
    Inbox and re-run code generation, overwriting the human-reviewed code.

    We only RESOLVE the existing row (recording the operator's choice); we never
    execute resolve_gate here, so no forge is triggered. Never raises into the
    dialog flow."""
    try:
        from systemu.approval.decision_queue import OperatorDecisionQueue
        queue = OperatorDecisionQueue(vault)
        dedup = f"forge:{tool_id}"
        match = next(
            (d for d in queue.list_pending() if d.dedup_key == dedup), None)
        if match is not None:
            queue.resolve(match.id, choice=choice)
    except Exception:
        logger.debug("[Tools UI] forge gate cleanup skipped", exc_info=True)


def _show_spec_review_dialog(tool_id: str) -> None:
    """Opens the two-gate spec→code review dialog for a PROPOSED tool."""
    state = AppState.get()
    try:
        tool = state.vault.get_tool(tool_id)
    except KeyError:
        ui.notify("Tool not found in vault.", type="negative")
        return

    # Mutable container for the generated code (shared between async callbacks)
    _pending_code: list[str] = []
    _edit_mode:    list[bool] = [False]   # True when code editor is active

    spec_fields = {
        "name":                 tool.name,
        "description":          tool.description,
        "tool_type":            str(tool.tool_type.value if hasattr(tool.tool_type, "value") else tool.tool_type),
        "parameters_schema":    tool.parameters_schema,
        "return_schema":        tool.return_schema,
        "implementation_notes": tool.implementation_notes,
        "dependencies":         tool.dependencies,
    }

    with ui.dialog() as dlg:
        dlg.props("persistent")
        with ui.card().style(
            f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
            f"border-radius: 16px; padding: 28px; min-width: 780px; max-width: 920px;"
        ):
            # ── Gate 1: Spec review ────────────────────────────────────────
            gate1_col = ui.column().classes("w-full")
            with gate1_col:
                ui.label("Gate 1 — Review Tool Specification").style(
                    f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 6px;"
                )
                ui.label(
                    "Edit the spec below if needed, then click Generate Code Preview."
                ).style(f"color: {THEME['text_muted']}; font-size: 13px; margin-bottom: 12px;")
                spec_area = ui.textarea(value=_json.dumps(spec_fields, indent=2)).style(
                    "width: 100%; font-family: monospace; font-size: 12px; min-height: 260px;"
                )

            # ── Gate 2: Code review (hidden until generated) ───────────────
            gate2_col = ui.column().classes("w-full")
            gate2_col.set_visibility(False)
            with gate2_col:
                ui.separator().style("margin: 16px 0;")
                ui.label("Gate 2 — Code Review").style(
                    f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 6px;"
                )
                g2_subtitle = ui.label(
                    "Read the generated code. Approve only if you are satisfied it is safe."
                ).style(f"color: {THEME['text_muted']}; font-size: 13px; margin-bottom: 10px;")

                # Read-only code view (default)
                code_block = ui.code("", language="python").style(
                    "width: 100%; max-height: 340px; overflow: auto; border-radius: 8px; font-size: 12px;"
                )
                # Editable code textarea (shown when Edit Code is active)
                code_editor = ui.textarea("").style(
                    "width: 100%; font-family: monospace; font-size: 12px; min-height: 300px;"
                )
                code_editor.set_visibility(False)

                risk_row = ui.row().classes("w-full flex-wrap gap-2").style("margin-top: 8px;")

                # Validation status row (shown after edit)
                validation_row = ui.row().classes("w-full items-center gap-2").style("margin-top: 6px;")
                validation_row.set_visibility(False)

            # ── Spinner ────────────────────────────────────────────────────
            spin_row = ui.row().classes("w-full justify-center items-center gap-3").style("margin-top: 12px;")
            spin_row.set_visibility(False)
            with spin_row:
                ui.spinner("dots", size="lg", color=THEME["primary"])
                ui.label("Generating code preview — this may take a moment…").style(
                    f"color: {THEME['text_muted']}; font-size: 13px;"
                )

            # ── Action buttons ─────────────────────────────────────────────
            with ui.row().style("gap: 10px; margin-top: 20px; justify-content: flex-end;"):
                gen_btn = ui.button("Generate Code Preview")
                gen_btn.style(f"background: {THEME['primary']}; color: white; border-radius: 8px;")

                approve_btn = ui.button("Approve & Sign Off")
                approve_btn.style(f"background: {THEME['success']}; color: white; border-radius: 8px;")
                approve_btn.set_visibility(False)

                # Third option A: edit the generated code inline
                edit_code_btn = ui.button("Edit Code")
                edit_code_btn.style(f"background: {THEME['warning']}; color: white; border-radius: 8px;")
                edit_code_btn.set_visibility(False)

                # Third option B: go back to spec and regenerate
                refine_btn = ui.button("Refine Spec")
                refine_btn.style(f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;")
                refine_btn.set_visibility(False)

                reject_btn = ui.button("Reject")
                reject_btn.style(f"background: {THEME['danger']}; color: white; border-radius: 8px;")
                reject_btn.set_visibility(False)

                ui.button("Cancel", on_click=dlg.close).style(
                    f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;"
                )

            # ── Event handlers ─────────────────────────────────────────────

            async def _on_generate():
                from systemu.pipelines.tool_forge import preview_tool_code

                # Validate edited spec JSON
                try:
                    edited = _json.loads(spec_area.value)
                except _json.JSONDecodeError as exc:
                    ui.notify(f"Invalid JSON in spec editor: {exc}", type="negative")
                    return

                # Apply edits to tool object (in memory)
                _apply_spec_edits(tool, edited)

                # Show spinner, hide Gate 1 + buttons
                gen_btn.set_visibility(False)
                gate1_col.set_visibility(False)
                spin_row.set_visibility(True)
                gate2_col.set_visibility(False)
                approve_btn.set_visibility(False)
                edit_code_btn.set_visibility(False)
                refine_btn.set_visibility(False)
                reject_btn.set_visibility(False)

                # Resolve scroll context (best-effort)
                scroll = _find_scroll_for_tool(tool, state)

                # Run synchronous LLM call in a thread to avoid blocking the event loop
                code = await asyncio.to_thread(preview_tool_code, tool, scroll, state.config)

                spin_row.set_visibility(False)

                if not code:
                    ui.notify("Code generation failed — check server logs.", type="negative")
                    gen_btn.set_visibility(True)
                    return

                # Stash for approval step
                _pending_code.clear()
                _pending_code.append(code)

                # Update code panel
                code_block.content = code

                # Advisory static scan (W2.1: AST-based; honest framing — a
                # static scan cannot prove code safe, so the empty state must
                # not read as a green light).
                risk_row.clear()
                with risk_row:
                    risks = _detect_risks(code)
                    if risks:
                        ui.label("⚠ Advisory static scan — flagged patterns (not a security verdict):").style(
                            f"color: {THEME['warning']}; font-size: 12px; font-weight: 600; width: 100%;"
                        )
                        for risk_label in risks:
                            ui.html(
                                f'<span style="background: color-mix(in srgb, {THEME["danger"]} 15%, transparent); '
                                f'color: {THEME["danger"]}; '
                                f'border: 1px solid color-mix(in srgb, {THEME["danger"]} 40%, transparent); '
                                f'border-radius: 4px; '
                                f'padding: 2px 8px; font-size: 11px; font-family: monospace;">'
                                f'{risk_label}</span>'
                            )
                    else:
                        ui.html(
                            f'<span style="color: {THEME["text_muted"]}; font-size: 12px;">'
                            f'Advisory static scan: no flagged patterns. This does not prove the '
                            f'code safe — read it before approving.</span>'
                        )

                gate2_col.set_visibility(True)
                approve_btn.set_visibility(True)
                edit_code_btn.set_visibility(True)
                refine_btn.set_visibility(True)
                reject_btn.set_visibility(True)
                _edit_mode[0] = False
                code_block.set_visibility(True)
                code_editor.set_visibility(False)
                validation_row.set_visibility(False)
                edit_code_btn.text = "Edit Code"

            def _on_approve():
                from systemu.pipelines.tool_forge import save_approved_code
                if not _pending_code:
                    ui.notify("Generate a preview first.", type="warning")
                    return
                # Structural validation before saving
                issues = _validate_tool_code(_pending_code[0])
                if issues:
                    ui.notify(
                        f"Code validation warning: {'; '.join(issues)}. "
                        "Fix via Edit Code or Refine Spec before approving.",
                        type="warning",
                    )
                    return
                try:
                    save_approved_code(tool, _pending_code[0], state.config, state.vault)
                    # Double-forge guard: this rich review path just forged the
                    # tool (human-reviewed code). Resolve the matching forge:
                    # Inbox gate as "Forge" so it can't be Approved again from
                    # the Inbox and re-run forge_tool_from_spec over this code.
                    _resolve_forge_gate_silently(tool.id, state.vault, choice="Forge")
                    ui.notify(
                        f"✓ '{tool.name}' forged. Installing deps & enabling…",
                        type="positive",
                    )
                    dlg.close()
                    # v0.8.13 Fix 6c: a reviewed approval also installs the tool's
                    # declared deps + enables it + heals waiting activities. pip
                    # install is blocking, so offload off the UI loop (same pattern
                    # as _toggle_enabled's _heal_async).
                    from nicegui import context as _nicegui_ctx
                    try:
                        _client = _nicegui_ctx.client
                    except Exception:
                        _client = None

                    async def _install_enable_async(t_id=tool.id, t_name=tool.name,
                                                    cfg=state.config, v=state.vault, c=_client):
                        try:
                            await asyncio.to_thread(_approve_install_and_enable, t_id, v, cfg)
                            if c is not None:
                                with c:
                                    ui.notify(
                                        f"'{t_name}' enabled — deps installed, waiting tasks resumed.",
                                        type="positive",
                                    )
                        except Exception as exc:
                            logger.exception("[Tools UI] approve-install-enable failed for %s", t_id)
                            if c is not None:
                                with c:
                                    ui.notify(
                                        f"'{t_name}' forged but install/enable failed: {exc}",
                                        type="warning",
                                    )

                    asyncio.create_task(_install_enable_async())
                except Exception as exc:
                    logger.exception("[Tools UI] save_approved_code failed")
                    ui.notify(f"Error saving tool: {exc}", type="negative")

            def _on_edit_code():
                if not _edit_mode[0]:
                    # Switch to edit mode
                    _edit_mode[0] = True
                    code_editor.value = _pending_code[0] if _pending_code else ""
                    code_block.set_visibility(False)
                    code_editor.set_visibility(True)
                    edit_code_btn.text = "Lock In Edits"
                    g2_subtitle.set_text(
                        "Edit the code below. Click 'Lock In Edits' to validate and apply changes."
                    )
                    validation_row.set_visibility(False)
                else:
                    # Validate and lock in edits
                    edited = code_editor.value
                    issues = _validate_tool_code(edited)
                    validation_row.clear()
                    validation_row.set_visibility(True)
                    with validation_row:
                        if issues:
                            ui.html(
                                f'<span style="color: {THEME["danger"]}; font-size: 12px; font-weight: 600;">'
                                f'✗ {"; ".join(issues)}</span>'
                            )
                            ui.notify("Fix the validation errors before locking in.", type="negative")
                            return  # Stay in edit mode
                        else:
                            ui.html(
                                f'<span style="color: {THEME["success"]}; font-size: 12px; font-weight: 600;">'
                                f'✓ Code validated — edits applied.</span>'
                            )
                    # Apply edits
                    _pending_code.clear()
                    _pending_code.append(edited)
                    code_block.content = edited
                    _edit_mode[0] = False
                    code_block.set_visibility(True)
                    code_editor.set_visibility(False)
                    edit_code_btn.text = "Edit Code"
                    g2_subtitle.set_text(
                        "Read the generated code. Approve only if you are satisfied it is safe."
                    )
                    # Re-run risk checker on edited code
                    risk_row.clear()
                    with risk_row:
                        risks = _detect_risks(edited)
                        if risks:
                            ui.label("⚠ Risk patterns detected — review carefully:").style(
                                f"color: {THEME['warning']}; font-size: 12px; font-weight: 600; width: 100%;"
                            )
                            for risk_label in risks:
                                ui.html(
                                    f'<span style="background: rgba(239,68,68,0.15); color: #ef4444; '
                                    f'border: 1px solid rgba(239,68,68,0.4); border-radius: 4px; '
                                    f'padding: 2px 8px; font-size: 11px; font-family: monospace;">'
                                    f'{risk_label}</span>'
                                )
                        else:
                            ui.html(
                                f'<span style="color: {THEME["success"]}; font-size: 12px;">'
                                f'✓ No dangerous patterns detected.</span>'
                            )

            def _on_refine_spec():
                # Return to Gate 1 — keep current spec text, hide Gate 2
                gate2_col.set_visibility(False)
                gate1_col.set_visibility(True)
                gen_btn.set_visibility(True)
                approve_btn.set_visibility(False)
                edit_code_btn.set_visibility(False)
                refine_btn.set_visibility(False)
                reject_btn.set_visibility(False)
                _pending_code.clear()
                _edit_mode[0] = False

            def _on_reject():
                # Resolve the matching forge: Inbox gate as "Skip" so the
                # rejected tool no longer surfaces an actionable forge card.
                _resolve_forge_gate_silently(tool.id, state.vault, choice="Skip")
                ui.notify(f"'{tool.name}' rejected — remains PROPOSED.", type="info")
                dlg.close()

            gen_btn.on("click", _on_generate)
            approve_btn.on("click", _on_approve)
            edit_code_btn.on("click", _on_edit_code)
            refine_btn.on("click", _on_refine_spec)
            reject_btn.on("click", _on_reject)

    dlg.open()


# ─────────────────────────────────────────────────────────────────────────────
#  Gate 3 — enable/disable toggle handler
# ─────────────────────────────────────────────────────────────────────────────

def _toggle_enabled(tool_id: str, enabled: bool, *, switch=None) -> None:
    """Gate 3: persist the user's enable/disable decision for a forged tool.

    P2-T11 consolidation: the **enable** branch routes through the ONE gated
    policy ``verbs.tools_enable`` (which itself delegates to the ONE mechanism
    ``tool_service.enable_tool`` — flip + FORGED→DEPLOYED + log). This means the
    toggle now ALSO enforces the Gate-3.5 dry-run gate; if the verb refuses
    (e.g. the tool's dry-run has not passed) we notify the verb's summary and
    revert the switch UI so it reflects the unchanged backing state.

    The **disable** branch is unchanged — it calls ``tool_service.disable_tool``
    directly (disable carries no gate). The heal chain (LLM calls) runs in a
    background thread to avoid blocking the NiceGUI event loop.
    """
    from nicegui import context as _nicegui_ctx
    from systemu.pipelines.tool_service import disable_tool, heal_activities_for_tool
    from systemu.interface.command import verbs
    state = AppState.get()
    try:
        tool = state.vault.get_tool(tool_id)
        tool_name = tool.name

        if enabled:
            # Route the enable through the single gated policy.
            result = verbs.tools_enable(tool_id, vault=state.vault)
            status = result.status.value
            if status == "noop":
                # Already enabled — the switch's ON state is already correct;
                # just report and skip the heal (nothing changed).
                ui.notify(result.summary, type="info")
                return
            if status != "ok":
                # Gate refused (ERROR/QUEUED) — surface the reason and revert
                # the toggle so the UI matches the unchanged (disabled) record.
                ui.notify(result.summary, type="negative")
                if switch is not None:
                    try:
                        switch.value = False
                    except Exception:
                        pass
                return

            # Dismiss stale forge notification and queue dependency advisory
            _resolve_forge_notification(tool_id, state.vault)
            _queue_dependency_reminder(tool, state.vault)

            # Capture NiceGUI client before the async boundary
            try:
                client = _nicegui_ctx.client
            except Exception:
                client = None

            async def _heal_async(t_id=tool_id, t_name=tool_name,
                                  cfg=state.config, v=state.vault, c=client):
                try:
                    await asyncio.to_thread(heal_activities_for_tool, t_id, cfg, v)
                    if c is not None:
                        with c:
                            ui.notify(
                                f"'{t_name}' enabled — shadow assignment complete.",
                                type="positive",
                            )
                except Exception as exc:
                    logger.exception("[Tools UI] Async heal failed for tool %s", t_id)
                    if c is not None:
                        with c:
                            ui.notify(
                                f"Tool enabled but shadow assignment failed: {exc}",
                                type="warning",
                            )

            asyncio.create_task(_heal_async())
            ui.notify(
                f"Tool '{tool_name}' enabled — assigning shadow in background.",
                type="positive",
            )
        else:
            disable_tool(tool_id, state.vault)
            ui.notify(f"Tool '{tool_name}' disabled.", type="info")

    except Exception as exc:
        logger.exception("[Tools UI] _toggle_enabled failed")
        ui.notify(f"Error updating tool: {exc}", type="negative")


def _resolve_forge_notification(tool_id: str, vault) -> None:
    """Dismiss any pending forge_tool notification for this tool_id."""
    try:
        for notif in vault.list_pending_notifications():
            if notif.get("context", {}).get("tool_id") == tool_id:
                vault.resolve_notification(notif["id"], "auto_dismissed")
    except Exception as exc:
        logger.debug("Failed to auto-resolve forge notification for %s: %s", tool_id, exc)


def _queue_dependency_reminder(tool, vault) -> None:
    from systemu.interface.notifications import queue_dependency_reminder
    queue_dependency_reminder(tool, vault)


# ─────────────────────────────────────────────────────────────────────────────
#  Manual forge dialog (unchanged workflow for brand-new tools)
# ─────────────────────────────────────────────────────────────────────────────

def _show_forge_dialog() -> None:
    """Dialog to forge a brand-new tool by name (enters same Gate 1→2 flow)."""
    with ui.dialog() as dlg, ui.card().style(
        f"background: {THEME['surface']}; border: 1px solid {THEME['border']}; "
        f"border-radius: 16px; padding: 28px; min-width: 420px;"
    ):
        ui.label("New Tool").style(
            f"font-size: 18px; font-weight: 700; color: {THEME['text']}; margin-bottom: 16px;"
        )
        name_input = ui.input(
            label="Tool name (snake_case)", placeholder="browser_navigate"
        ).style("width: 100%;")
        ctx_input = ui.textarea(
            label="Context / description", placeholder="What this tool should do…"
        ).style("width: 100%;")

        def _do_create():
            from systemu.core.models import Tool as ToolModel, ToolStatus, ToolType
            from systemu.core.utils import generate_id

            tool_name = name_input.value.strip()
            if not tool_name:
                ui.notify("Tool name is required.", type="warning")
                return

            state = AppState.get()

            # Check for duplicate
            existing = state.vault.find_tool_by_name(tool_name)
            if existing:
                ui.notify(
                    f"A tool named '{tool_name}' already exists — open it from the table.",
                    type="warning",
                )
                dlg.close()
                return

            # Register as PROPOSED so the review dialog can open it
            stub = ToolModel(
                id=generate_id("tool"),
                name=tool_name,
                description=ctx_input.value.strip(),
                tool_type=ToolType.PYTHON_FUNCTION,
                implementation_notes=ctx_input.value.strip(),
                status=ToolStatus.PROPOSED,
                forged_by_systemu=True,
            )
            state.vault.save_tool(stub)
            dlg.close()
            ui.notify(
                f"Tool '{tool_name}' registered. Opening spec review…",
                type="positive",
            )
            _show_spec_review_dialog(stub.id)

        with ui.row().style("gap: 10px; margin-top: 16px;"):
            ui.button("Continue to Spec Review", on_click=_do_create).style(
                f"background: {THEME['primary']}; color: white; border-radius: 8px;"
            )
            ui.button("Cancel", on_click=dlg.close).style(
                f"background: {THEME['surface2']}; color: {THEME['text']}; border-radius: 8px;"
            )
    dlg.open()


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _td(text: str, bold: bool = False) -> None:
    style = (
        f"padding: 12px 16px; border-bottom: 1px solid {THEME['border']}; "
        f"color: {THEME['text']}; font-size: 14px;"
        + (" font-weight: 600;" if bold else "")
    )
    with ui.element("td").style(style):
        ui.label(text)


def _apply_spec_edits(tool, edited: dict) -> None:
    """Apply user-edited spec JSON fields to the tool object (in memory)."""
    from systemu.core.models import ToolType
    if "name" in edited:
        tool.name = edited["name"]
    if "description" in edited:
        tool.description = edited["description"]
    if "parameters_schema" in edited:
        tool.parameters_schema = edited["parameters_schema"]
    if "return_schema" in edited:
        tool.return_schema = edited["return_schema"]
    if "implementation_notes" in edited:
        tool.implementation_notes = edited["implementation_notes"]
    if "dependencies" in edited:
        tool.dependencies = edited["dependencies"]
    if "tool_type" in edited:
        try:
            tool.tool_type = ToolType(edited["tool_type"])
        except ValueError:
            pass


def _find_scroll_for_tool(tool, state) -> object:
    """Look up the scroll associated with this tool via activity index; stub on miss."""
    from systemu.core.models import Scroll as ScrollModel
    try:
        for a_header in state.vault.list_activities():
            if tool.id in (a_header.get("required_tool_ids") or []):
                act    = state.vault.get_activity(a_header["id"])
                return state.vault.get_scroll(act.scroll_id)
    except Exception:
        pass  # best-effort lookup — falls through to the stub Scroll below
    return ScrollModel(
        id="stub",
        name=tool.name,
        source_session_id="ui",
        raw_instructions_path="",
        narrative_md=tool.description or tool.name,
    )


def _detect_risks(code: str) -> list[str]:
    """ADVISORY scan of generated code (AST-based; W2.1).

    Returns human-readable labels.  Delegates to runtime.code_risk so the
    CLI / any future surface shares the one scanner.  Advisory by nature —
    surfaces must not present an empty result as proof of safety.
    """
    from systemu.runtime.code_risk import scan_code
    return scan_code(code)


def _validate_tool_code(code: str) -> list[str]:
    """Structurally validate tool code. Returns a list of issues (empty = valid).

    Checks:
      1. Valid Python syntax (ast.parse)
      2. TOOL_META dict present at module level
      3. run() function defined
    """
    import ast
    issues: list[str] = []

    # 1. Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        issues.append(f"Syntax error on line {exc.lineno}: {exc.msg}")
        return issues  # No point checking structure if syntax is broken

    # 2. TOOL_META assignment exists
    has_tool_meta = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "TOOL_META"
            for t in node.targets
        )
        for node in ast.walk(tree)
    )
    if not has_tool_meta:
        issues.append("Missing TOOL_META dict at module level")

    # 3. run() function defined
    has_run = any(
        isinstance(node, ast.FunctionDef) and node.name == "run"
        for node in ast.walk(tree)
    )
    if not has_run:
        issues.append("Missing run() function")

    return issues
