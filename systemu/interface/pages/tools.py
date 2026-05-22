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

# Dangerous patterns the risk checker scans for in generated code
_RISK_PATTERNS: dict[str, str] = {
    "shell=True":      "subprocess shell injection (shell=True)",
    "os.system":       "os.system() call",
    "os.popen":        "os.popen() call",
    "eval(":           "eval() — arbitrary code execution",
    "exec(":           "exec() — arbitrary code execution",
    "__import__":      "__import__() — dynamic import bypass",
    "shutil.rmtree":   "recursive directory deletion (shutil.rmtree)",
    " rm -rf":         "rm -rf in a string literal",
    "DROP TABLE":      "SQL DROP TABLE",
    "DELETE FROM":     "SQL DELETE FROM",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Page builder
# ─────────────────────────────────────────────────────────────────────────────

def _render_unbaked_banner() -> None:
    """surface runtime-approved deps that haven't been baked into
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
    with ui.row().classes("w-full bg-blue-50 border-l-4 border-blue-400 p-3 q-mb-md"):
        ui.icon("info").classes("text-blue-700 text-xl")
        ui.label(
            f"{len(unbaked)} runtime-approved dep(s) not yet baked into image "
            f"({names}). Next `docker compose build` picks them up."
        ).classes("text-blue-900")


def build_tools_page() -> None:
    state = AppState.get()
    vault = state.vault

    _render_unbaked_banner()

    with ui.row().classes("w-full items-center justify-between").style("margin-bottom: 20px;"):
        ui.label("🔧 Tool Registry").style(
            f"font-size: 22px; font-weight: 800; color: {THEME['text']};"
        )
        ui.button("+ New Tool", on_click=_show_forge_dialog).style(
            f"background: {THEME['primary']}; color: white; border-radius: 8px;"
        )

    # Tool-dependency approval card (v0.3.4).  Appears above the registry
    # table so the operator sees pending pip-install requests before
    # digging through the tool list.
    with ui.column().classes("w-full").style("margin-bottom: 24px;"):
        ui.label("📦 Tool Dependencies").style(
            f"font-size: 14px; color: {THEME['text_muted']}; font-weight: 700; "
            f"letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 8px;"
        )
        from systemu.interface.components.pending_deps import build_pending_deps
        build_pending_deps(compact=False)

    # Recalibration approval cards (v0.5.0-e).  Pulled from EventBus
    # buffer and rendered with action buttons; only shows when there
    # are recalibrations awaiting decision.
    _build_recalibration_cards(vault)

    tools = vault.load_index("tools")
    if not tools:
        ui.label("No tools yet. Process a scroll to auto-discover tools.").style(
            f"color: {THEME['text_muted']}; font-style: italic; padding: 20px;"
        )
        return

    with ui.element("table").style(
        f"width: 100%; border-collapse: collapse; background: {THEME['surface']}; "
        f"border: 1px solid {THEME['border']}; border-radius: 12px; overflow: hidden;"
    ):
        with ui.element("thead"):
            with ui.element("tr"):
                for col in ["Name", "Type", "Status", "Enabled", "Dry-run", "Success", "Description", "Actions"]:
                    with ui.element("th").style(
                        f"background: {THEME['surface2']}; color: {THEME['text_muted']}; "
                        f"font-size: 11px; font-weight: 600; text-transform: uppercase; "
                        f"letter-spacing: 0.08em; padding: 10px 16px; text-align: left; "
                        f"border-bottom: 1px solid {THEME['border']};"
                    ):
                        ui.label(col)

        with ui.element("tbody"):
            for t in tools:
                tid     = t["id"]
                status  = t.get("status", "")
                enabled = t.get("enabled", False)

                with ui.element("tr"):
                    _td(t.get("name", tid), bold=True)
                    _td(t.get("tool_type", "—"))

                    # Status badge — FORGED+enabled gets a green override
                    with ui.element("td").style("padding: 12px 16px;"):
                        if status == "forged" and enabled:
                            ui.html(status_badge_html("enabled"))
                        else:
                            ui.html(status_badge_html(status or "?"))

                    # Enabled toggle (Gate 3) — only for tools that have been reviewed
                    with ui.element("td").style("padding: 12px 16px;"):
                        if status in ("forged", "deployed", "tested", "upgraded"):
                            sw = ui.switch("", value=enabled).props("dense")
                            sw.on(
                                "update:model-value",
                                lambda e, i=tid: _toggle_enabled(
                                    i,
                                    # NiceGUI may deliver args as a raw bool OR as a
                                    # list/tuple wrapping the bool — normalise both.
                                    e.args if isinstance(e.args, bool)
                                    else bool(e.args[0]) if isinstance(e.args, (list, tuple)) and e.args
                                    else bool(e.args),
                                ),
                            )
                        else:
                            ui.label("—").style(f"color: {THEME['text_muted']}; font-size: 12px;")

                    # Dry-run status column (v0.5.0-a) — passed / failed / skipped / not_run.
                    with ui.element("td").style("padding: 12px 16px;"):
                        dr_status = t.get("dry_run_status") or "not_run"
                        dr_color = {
                            "passed":  THEME["success"],
                            "failed":  THEME["danger"],
                            "skipped": THEME["warning"],
                            "not_run": THEME["text_muted"],
                        }.get(dr_status, THEME["text_muted"])
                        ui.label(dr_status).style(
                            f"font-size: 11px; color: {dr_color}; font-weight: 700; "
                            f"text-transform: uppercase; letter-spacing: 0.05em;"
                        )

                    # Success-rate column (v0.4.4-b) — pulls from ToolMetrics.
                    with ui.element("td").style("padding: 12px 16px;"):
                        try:
                            from systemu.runtime.tool_metrics import get_tool_metrics
                            entry = get_tool_metrics().get(tid)
                            if entry.has_history and entry.attributable_calls > 0:
                                rate = entry.success_rate
                                rate_color = (
                                    THEME["success"] if rate >= 0.7
                                    else THEME["warning"] if rate >= 0.4
                                    else THEME["danger"]
                                )
                                ui.label(f"{rate*100:.0f}% ({entry.attributable_calls})").style(
                                    f"font-size: 12px; color: {rate_color}; "
                                    f"font-weight: 700;"
                                )
                            else:
                                ui.label("—").style(
                                    f"color: {THEME['text_muted']}; font-size: 12px;"
                                )
                        except Exception:
                            ui.label("—").style(
                                f"color: {THEME['text_muted']}; font-size: 12px;"
                            )

                    desc = t.get("description", "") or ""
                    _td(desc[:70] + "…" if len(desc) > 70 else desc or "—")

                    # Actions column
                    with ui.element("td").style("padding: 12px 16px;"):
                        if status == "proposed":
                            ui.button(
                                "Review & Forge",
                                on_click=lambda _, i=tid: _show_spec_review_dialog(i),
                            ).style(
                                f"background: {THEME['warning']}; color: white; "
                                f"border-radius: 6px; font-size: 12px; padding: 4px 10px;"
                            )
                        else:
                            ui.label("—").style(f"color: {THEME['text_muted']}; font-size: 12px;")


# ─────────────────────────────────────────────────────────────────────────────
#  Gate 1 → Gate 2 spec/code review dialog
# ─────────────────────────────────────────────────────────────────────────────

def _build_recalibration_cards(vault) -> None:
    """surface pending RECALIBRATE_TOOL approvals on the Tools page.

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

    ui.label("🔁 Pending Tool Recalibrations").style(
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
        # spec diff (collapsible).  Shows what changed in the
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
            # operator override actions.  When the supervisor
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
    """— enable the new tool + ask the Supervisor to resume."""
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
            "message":  f"🔁 Recalibration approved → submission {sub_id}",
            "context": {
                "dedup_key": ctx.get("dedup_key"),
                "outcome":   "approved",
            },
        })
    except Exception:
        pass
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
    """— operator forces a different recalibration mode.

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
                "message":  f"🔁 Original recalibration dismissed (operator overrode → {forced_mode})",
                "context": {
                    "dedup_key": ctx.get("dedup_key"),
                    "outcome":   "overridden",
                },
            })
        except Exception:
            pass
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
    except Exception:
        pass
    try:
        from systemu.interface.event_bus import EventBus
        from datetime import datetime, timezone
        EventBus.get().publish({
            "ts":       datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "level":    "WARNING",
            "category": "approval_dismissed",
            "message":  "🔁 Recalibration rejected by operator",
            "context": {
                "dedup_key": ctx.get("dedup_key"),
                "outcome":   "rejected",
            },
        })
    except Exception:
        pass
    ui.notify("Recalibration rejected. Supervisor will avoid similar proposals.",
               type="warning")


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
                edit_code_btn = ui.button("✏️ Edit Code")
                edit_code_btn.style(f"background: {THEME['warning']}; color: white; border-radius: 8px;")
                edit_code_btn.set_visibility(False)

                # Third option B: go back to spec and regenerate
                refine_btn = ui.button("🔄 Refine Spec")
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

                # Risk checker
                risk_row.clear()
                with risk_row:
                    risks = _detect_risks(code)
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

                gate2_col.set_visibility(True)
                approve_btn.set_visibility(True)
                edit_code_btn.set_visibility(True)
                refine_btn.set_visibility(True)
                reject_btn.set_visibility(True)
                _edit_mode[0] = False
                code_block.set_visibility(True)
                code_editor.set_visibility(False)
                validation_row.set_visibility(False)
                edit_code_btn.text = "✏️ Edit Code"

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
                    ui.notify(
                        f"✓ '{tool.name}' forged. Enable it in the registry when ready.",
                        type="positive",
                    )
                    dlg.close()
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
                    edit_code_btn.text = "🔒 Lock In Edits"
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
                    edit_code_btn.text = "✏️ Edit Code"
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

def _toggle_enabled(tool_id: str, enabled: bool) -> None:
    """Gate 3: persist the user's enable/disable decision for a forged tool.

    Delegates state changes to tool_service so the FORGED ↔ DEPLOYED
    transition and heal chain are never skipped regardless of call site.
    The heal chain (LLM calls) runs in a background thread to avoid
    blocking the NiceGUI event loop.
    """
    from nicegui import context as _nicegui_ctx
    from systemu.pipelines.tool_service import enable_tool, disable_tool, heal_activities_for_tool
    state = AppState.get()
    try:
        tool = state.vault.get_tool(tool_id)
        tool_name = tool.name

        if enabled:
            enable_tool(tool_id, state.vault)

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
    except Exception:
        pass


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
        pass
    return ScrollModel(
        id="stub",
        name=tool.name,
        source_session_id="ui",
        raw_instructions_path="",
        narrative_md=tool.description or tool.name,
    )


def _detect_risks(code: str) -> list[str]:
    """Scan generated code for dangerous patterns. Returns human-readable labels."""
    return [label for pattern, label in _RISK_PATTERNS.items() if pattern in code]


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
