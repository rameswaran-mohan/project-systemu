"""Side-by-side remediation card (Phase 5 Slice 2c).

Replaces the dead recovery-panel row and kills the self-referential fix_url
loop: ``RecoveryAction.fix_url`` is ALWAYS ``/recover/<scope>/<id>`` for EVERY
kind (engine._make → links.recover_url), so the old gate-review button
redirected to a page rendering the same row with the same button.  The card
maps each kind to exactly ONE remediation affordance instead:

  apply — {DEP_PENDING, GATE_3_DISABLED, MEMORY_POISONED} → the existing
          ``recover._handle_action`` apply path (THE single shared apply path,
          unchanged);
  gate  — {GATE_1_PENDING, GATE_2_PENDING} → enqueue-on-demand recovery gate
          (``GateDescriptor.from_recovery_action``, gate_type="recovery",
          policy=None — mirrors ``scroll_gate.ensure_scroll_gate``) reviewed
          through the unified Inbox card in a dialog;
  none  — {SKILL_MISSING, FS_PERMISSION, DRY_RUN_FAILED_BUG} + unknown kinds
          → guidance text only, no button.

NO fix-url redirection anywhere.  Import-light: NiceGUI is imported only
inside the render/dialog functions, so the pure model and
``ensure_recovery_gate`` are testable headless.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

# The ONE severity→risk map (mirrors recovery.engine.Severity).
from systemu.interface.command.gate import _SEVERITY_TO_RISK

_APPLY_KINDS = frozenset({"DEP_PENDING", "GATE_3_DISABLED", "MEMORY_POISONED"})
_GATE_KINDS = frozenset({"GATE_1_PENDING", "GATE_2_PENDING"})

# Guidance text for kinds with no automated fix (and unknown future kinds).
_GUIDANCE: Dict[str, str] = {
    "SKILL_MISSING": ("A referenced skill is missing — re-link or recreate "
                      "it from the Skills page, then re-run diagnosis."),
    "FS_PERMISSION": ("Fix the filesystem permission on the path named in "
                      "the reason, then re-run diagnosis."),
    "DRY_RUN_FAILED_BUG": ("The dry-run hit a code bug — review the error "
                           "and rebuild the tool in the Forge."),
}
_GUIDANCE_DEFAULT = ("No automated fix for this finding — review the reason "
                     "and resolve it manually.")


def remediation_card_model(action) -> dict:
    """Pure view-model for one RecoveryAction (UI-free / headless-testable).

    ``fix.button`` is the ONE affordance: "apply" | "gate" | "none".
    ``fix_url`` deliberately never survives into the model — that link is
    self-referential by construction (the loop this card kills).
    """
    kind = action.kind
    if kind in _APPLY_KINDS:
        button = "apply"
    elif kind in _GATE_KINDS:
        button = "gate"
    else:
        button = "none"
    return {
        "kind": kind,
        "severity": action.severity,
        "risk": _SEVERITY_TO_RISK.get(action.severity, "low"),
        "problem": {"title": kind, "reason": action.reason},
        "fix": {
            "text": (action.fix_command
                     or _GUIDANCE.get(kind, _GUIDANCE_DEFAULT)),
            "button": button,
        },
    }


# ── gate-kind plumbing (mirrors scroll_gate.ensure_scroll_gate, Slice 2b) ────

def ensure_recovery_gate(vault, action) -> str:
    """Return the pending decision id for ``action``'s recovery gate,
    enqueueing it on demand when missing.

    Idempotent twice over: we pre-check ``list_descriptors`` for the
    recovery-scoped dedup key (``recovery:<scope>:<id>:<kind>``), and
    ``OperatorDecisionQueue.post`` itself dedups pending rows on dedup_key —
    double-clicks can never produce two gates.

    NEVER auto-executes: ``policy=None`` skips the gate-mode dial entirely,
    so even under ``SYSTEMU_GATE_MODE=bypass`` this POSTS a pending row for
    the operator to inspect (they explicitly asked to review, so Bypass's
    auto-grant must not apply here).
    """
    from systemu.interface.command.gate import GateDescriptor
    from systemu.interface.command.inbox import InboxQueue

    queue = InboxQueue(vault)
    dedup = f"recovery:{action.scope_kind}:{action.scope_id}:{action.kind}"
    for dec_id, descriptor in queue.list_descriptors():
        if getattr(descriptor, "dedup", "") == dedup:
            return dec_id
    descriptor = GateDescriptor.from_recovery_action(action)
    return queue.enqueue(descriptor, gate_type="recovery", policy=None)


def open_recovery_review_dialog(
    action, *, vault, on_resolved: Optional[Callable[[], None]] = None,
) -> None:
    """Open the inspect-before-approve dialog for a gate-kind RecoveryAction.

    Ensures the gate row exists (``ensure_recovery_gate``), then renders the
    EXISTING unified Inbox card (``inbox_page._render_unified_card``) inside
    a dialog — its buttons run the proven resolve chain
    (``queue.resolve`` → ``resolve_gate`` → ``doctor_apply``).
    """
    from nicegui import ui

    from systemu.interface.command.inbox import InboxQueue
    from systemu.interface.design import card
    from systemu.interface.pages.inbox_page import _render_unified_card

    try:
        dec_id = ensure_recovery_gate(vault, action)
    except Exception as exc:
        ui.notify(f"Could not open the recovery gate: {exc}", type="negative")
        return

    descriptor = next(
        (d for i, d in InboxQueue(vault).list_descriptors() if i == dec_id),
        None)
    if descriptor is None:
        # Race: resolved between ensure and render — nothing left to review.
        ui.notify("Gate already resolved — refreshing.", type="warning")
        if on_resolved is not None:
            on_resolved()
        return

    with ui.dialog() as dlg, card(classes="s-dialog q-pa-lg"):
        def _resolved() -> None:
            dlg.close()
            if on_resolved is not None:
                on_resolved()

        _render_unified_card(dec_id, descriptor, vault=vault,
                             on_resolved=_resolved)
        ui.button("Close", on_click=dlg.close).classes(
            "s-btn s-btn--ghost q-mt-md")
    dlg.open()


# ── rendering: the side-by-side card ─────────────────────────────────────────

def render_remediation_card(
    action, *, vault, on_applied: Optional[Callable[[], None]] = None,
) -> None:
    """Render one RecoveryAction as a side-by-side Problem | Fix card.

    apply → the existing ``recover._handle_action`` (THE single shared apply
    path — imported lazily so recover.py can delegate here without a cycle);
    gate → ``open_recovery_review_dialog`` (enqueue-on-demand + unified card);
    none → guidance text only.
    """
    from nicegui import ui
    from systemu.interface.design.primitives import button, card, status_pill

    model = remediation_card_model(action)

    def _apply(_=None, act=action):
        from systemu.interface.pages.recover import _handle_action
        try:
            _handle_action(act)
            ui.notify(f"Applied: {act.kind}", type="positive")
        except Exception as exc:
            ui.notify(f"Failed: {exc}", type="negative")
        if on_applied is not None:
            on_applied()

    def _review(_=None, act=action):
        open_recovery_review_dialog(act, vault=vault, on_resolved=on_applied)

    with card(classes="w-full q-mb-sm"):
        with ui.row().classes("w-full no-wrap items-start q-gutter-md"):
            # Problem column.
            with ui.column().classes("col").style("gap: 4px;"):
                ui.label("PROBLEM").classes("s-field-label")
                with ui.row().classes("items-center").style("gap: 8px;"):
                    status_pill(model["risk"])
                    ui.label(model["problem"]["title"]).classes(
                        "s-cell s-cell--bold")
                ui.label(model["problem"]["reason"]).classes("s-cell").style(
                    "white-space: pre-wrap;")
            # Fix column: the text + the ONE button (or none).
            with ui.column().classes("col").style("gap: 4px;"):
                ui.label("FIX").classes("s-field-label")
                ui.label(model["fix"]["text"]).classes("s-mono").style(
                    "white-space: pre-wrap;")
                if model["fix"]["button"] == "apply":
                    button("Approve & Apply", variant="primary",
                           on_click=_apply)
                elif model["fix"]["button"] == "gate":
                    button("Review Gate", variant="primary",
                           on_click=_review)
