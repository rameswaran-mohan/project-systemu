"""Dashboard recovery panel: /recover/<scope>/<scope_id>"""
from __future__ import annotations
from typing import List

from nicegui import ui

from systemu.recovery.engine import RecoveryEngine, RecoveryAction


def _get_vault():
    from systemu.interface.dashboard_state import AppState
    return AppState.get().vault


def _diagnose(scope: str, scope_id: str) -> List[RecoveryAction]:
    if scope not in {"tool", "shadow", "scroll", "activity"}:
        return []
    eng = RecoveryEngine(vault=_get_vault())
    method = {
        "tool": eng.diagnose_tool,
        "shadow": eng.diagnose_shadow,
        "scroll": eng.diagnose_scroll,
        "activity": eng.diagnose_activity,
    }[scope]
    return method(scope_id)


def _severity_color(sev: str) -> str:
    return {"blocker": "red", "warning": "amber", "info": "blue"}.get(sev, "grey")


def render_recovery_panel(scope: str, scope_id: str) -> None:
    """Reusable component — embed inside any page."""
    from systemu.interface.name_resolver import resolve_name, short_id

    actions = _diagnose(scope, scope_id)
    _v = _get_vault()
    with ui.card().classes("w-full"):
        ui.label(f"Recovery: {scope} {resolve_name(scope_id, _v)}").classes("text-h6")
        ui.label(short_id(scope_id)).style(
            "color: #94a3b8; font-size: 11px; font-family: monospace;"
        )
        if not actions:
            ui.label("No pending actions").classes("text-green-700")
            return
        for a in actions:
            _render_action_row(a)


def _render_action_row(a: RecoveryAction) -> None:
    with ui.row().classes("w-full items-center"):
        ui.icon("warning", color=_severity_color(a.severity)).classes("text-2xl")
        with ui.column().classes("flex-grow"):
            ui.label(f"{a.kind} - {a.reason}").classes("text-body1")
            if a.fix_command:
                ui.label(a.fix_command).classes("text-mono text-caption")
        if a.kind in {"DEP_PENDING", "GATE_3_DISABLED", "MEMORY_POISONED"}:
            ui.button(
                "Approve & Apply",
                on_click=lambda act=a: _handle_action_and_notify(act),
            ).props("color=primary")
        else:
            ui.button(
                "Open Gate Review",
                on_click=lambda url=a.fix_url: ui.navigate.to(url),
            )


def _handle_action_and_notify(a: RecoveryAction) -> None:
    try:
        _handle_action(a)
        ui.notify(f"Applied: {a.kind}", type="positive")
    except Exception as e:
        ui.notify(f"Failed: {e}", type="negative")


# ---- per-kind dispatch (Task 8) ----

def _extract_missing_package(reason: str) -> str:
    if "missing package:" in reason:
        return reason.split("missing package:", 1)[1].strip()
    return ""


def _dispatch_install_dep(tool_id: str, package: str, *, vault=None):
    # ``vault`` accepted for signature uniformity (the headless CLI apply path
    # threads its own vault through ``_handle_action``); dep approval resolves
    # the backing store itself, so it is unused here.
    # v0.9: the unified Decisions Inbox is now the primary dep-approval surface;
    # this remains a working install-once fallback for the /recover apply path
    # (the gate dedups on dep:<package>, so it won't double-install).
    from systemu.runtime.dep_approvals import approve_and_install
    approve_and_install(tool_id=tool_id, package=package, source="dashboard")


def _dispatch_enable_tool(tool_id: str, *, vault=None):
    """Flip Gate 3 to enabled — routed through the ONE gated policy (P2-T11).

    Consolidation: instead of mutating the vault directly, this dispatcher now
    calls ``verbs.tools_enable`` (the single gated policy, which delegates to the
    single mechanism ``tool_service.enable_tool``). So the recovery apply path
    gets the SAME Gate-3.5 dry-run gate + FORGED→DEPLOYED advance + event log as
    the CLI and the dashboard toggle — no fourth divergent writer.

    GATE_3_DISABLED is only emitted for runtime-ready tools (deployed / tested /
    upgraded), and every writer that reaches those statuses requires a passed
    dry-run, so the gate is a no-op for the legitimate recovery case. A genuinely
    un-dry-run-passed tool is now correctly refused rather than silently enabled.

    ``vault`` defaults to ``_get_vault()`` (the dashboard ``AppState`` vault) so
    existing web callers are unchanged; the headless ``doctor --apply`` path
    passes its own vault since ``AppState`` is not initialised in a CLI
    subprocess. Behaviour-equivalent for ``_handle_action`` (which ignores the
    return value and only surfaces exceptions)."""
    if vault is None:
        vault = _get_vault()
    from systemu.interface.command import verbs
    return verbs.tools_enable(tool_id, vault=vault)


def _dispatch_reset_memory(shadow_id: str, keep_successes: bool, *, vault=None):
    if vault is None:
        vault = _get_vault()
    from systemu.runtime.memory_consolidator import reset_shadow_memory
    reset_shadow_memory(shadow_id=shadow_id, keep_successes=keep_successes,
                        vault=vault)


def _handle_action(a: RecoveryAction, *, vault=None) -> None:
    """Apply one RecoveryAction. THE single apply path for both surfaces.

    ``vault`` defaults to ``None`` so the web recovery panel keeps its current
    behaviour: when no vault is injected the dispatchers are called with their
    original positional signature (and each falls back to ``_get_vault()``).
    The headless ``doctor --apply`` path passes its own vault — ``AppState`` is
    not initialised in a CLI subprocess, so ``_get_vault()`` would raise.

    The ``vault=`` kwarg is threaded through only when a vault was injected so
    that existing callers/tests that patch the dispatchers with the old
    positional-only signature keep working unchanged."""
    kw = {} if vault is None else {"vault": vault}
    if a.kind == "DEP_PENDING":
        pkg = _extract_missing_package(a.reason)
        if pkg:
            _dispatch_install_dep(a.scope_id, pkg, **kw)
    elif a.kind == "GATE_3_DISABLED":
        _dispatch_enable_tool(a.scope_id, **kw)
    elif a.kind == "MEMORY_POISONED":
        _dispatch_reset_memory(a.scope_id, keep_successes=True, **kw)
    # GATE_1_PENDING / GATE_2_PENDING route via fix_url, not handled here.


@ui.page("/recover/{scope}/{scope_id}")
def recover_page(scope: str, scope_id: str):
    if scope not in {"tool", "shadow", "scroll", "activity"}:
        ui.label(f"Unknown scope: {scope}").classes("text-red")
        return
    render_recovery_panel(scope, scope_id)
