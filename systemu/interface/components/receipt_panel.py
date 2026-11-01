"""UX-10 Evidence Receipts -- the shared DEC-13 badge and the receipt behind it.

The badge is ONE component on every surface that renders an outcome (PAR-2): the
Work row, the workflow detail page, the chat card and Home all call
:func:`render_badge` with the value the mint returned
(``runtime.verification_badge.run_badge``). The badge is a button; one click opens
the receipt panel:

  * **verified**     -- per effect: what was checked, how, when (local time), where
                        and the evidence fingerprint;
  * **claimed**      -- the anti-receipt: exactly what was NOT verified, and why;
  * **measured**     -- the shadow meter's record-only measurement, and whether it
                        would have credited;
  * **handoff / failed** -- the committed-effects list (IMPL-7), each effect badged;
  * **not recorded** -- the record could not be read, and why. Nothing verified.

Hard rules (UX-10): the panel renders PERSISTED state only. It is built from the
value the surface already minted, so opening it reads no file, makes no network
call and never re-verifies (asserted). Every persisted string is masked through
the mint's display helpers, which reach the shared span primitive
(``gateway.mask_secret_spans``) through the durable mask, before it is rendered.
The receipt keeps no host, submission token or artifact body, so those rows say
"not recorded by this receipt" rather than inventing one (DEC-27). The panel lives
on the dashboard origin, which the capture exclusion already refuses
(``runtime.capture_exclusion``). Token classes only; ASCII operator copy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from systemu.runtime import verification_badge as vb

#: Collapse a receipt note beyond this many lines behind an expander.
EXCERPT_LINES = 20

_NOT_IN_RECEIPT = "not recorded by this receipt"

_TITLE = {
    vb.VERIFIED: "Receipt: independently verified",
    vb.CLAIMED: "Anti-receipt: what was NOT verified",
    vb.MEASURED: "Receipt: measured, not credited",
    vb.FAILED: "Receipt: the run did not finish",
    vb.HANDOFF: "Receipt: handed back to you",
    vb.NOT_RECORDED: "Receipt: not recorded",
}


def _when(stamped_at: Any) -> str:
    if type(stamped_at) is not str or not stamped_at.strip():
        return _NOT_IN_RECEIPT
    from systemu.interface.ui_helpers import format_stamp
    shown = format_stamp(stamped_at)
    return f"{shown} (local time)" if shown else _NOT_IN_RECEIPT


def _fingerprint(effect: vb.EffectBadge) -> str:
    from systemu.runtime import ledger
    try:
        return ledger.receipt_fingerprint(effect)
    except Exception:
        return _NOT_IN_RECEIPT


def _effect_receipt(e: vb.EffectBadge) -> Dict[str, Any]:
    v = vb.view(e)
    note = vb.detail_text(e.detail)
    out: Dict[str, Any] = {
        "state": v.state, "label": v.label, "icon": v.icon,
        "pill_class": v.pill_class, "tooltip": v.tooltip,
        "objective": f"Objective {vb.objective_text(e.objective_id)}",
        "note_lines": note.splitlines() if note else [],
        "rows": [],
        "anti": "",
    }
    if v.state == vb.NOT_RECORDED:
        out["anti"] = ("This receipt entry could not be read, so nothing about this "
                       "effect is shown as verified.")
        return out
    out["rows"] = [
        ("What was checked", "see the note below" if note else _NOT_IN_RECEIPT),
        ("How", vb.method_text(e.method)),
        ("When", _when(e.stamped_at)),
        ("Where (host)", _NOT_IN_RECEIPT),
        ("Submission token", _NOT_IN_RECEIPT),
        ("Artifact", "not kept - the durable receipt stores no artifact body; "
                     "its fingerprint is retained"),
        ("Fingerprint", _fingerprint(e)),
    ]
    if v.state == vb.CLAIMED:
        out["anti"] = ("Not independently verified: no read-back confirmed this "
                       "effect. It stays the tool's own report.")
    elif v.state == vb.MEASURED:
        out["anti"] = ("Record-only measurement by the S4 shadow meter; it did not "
                       "credit this run. It would " + ("" if e.would_credit else "not ")
                       + "have credited.")
    return out


def _record_note(b: vb.RunBadge) -> str:
    if b.record == vb.RECORD_UNREADABLE:
        return ("Committed effects: not recorded - the receipt record could not be "
                "read (" + (b.reason or "no reason recorded") + ").")
    if b.record == vb.RECORD_NO_RUN:
        return "No execution is recorded for this run, so no receipt can exist."
    if not b.effects:
        return "No external effect is on record for this run."
    return ""


def _anti_receipt(b: vb.RunBadge) -> List[str]:
    if b.state != vb.CLAIMED:
        return []
    if not b.effects:
        return ["No independent artifact exists for this run: no read-back, "
                "confirmation email, page re-check or attestation is on record.",
                "Its outcome is the agent's own report. Local checks may have "
                "passed; they are not an independent confirmation."]
    n, total = b.count(vb.CLAIMED), len(b.effects)
    return [f"Not independently confirmed: {n} of {total} recorded "
            f"effect{'s' if total != 1 else ''} (each is marked below)."]


def receipt_panel_model(badge: Any) -> Dict[str, Any]:
    """Pure: the UX-10 receipt for one minted run badge. Persisted data only;
    no file read, no network. Anything that is not a minted ``RunBadge`` renders
    the not-recorded receipt (DEC-37)."""
    v = vb.view(badge)
    if type(badge) is not vb.RunBadge or not v.show:
        return {"state": v.state, "show": v.show, "label": v.label, "icon": v.icon,
                "pill_class": v.pill_class, "title": _TITLE.get(v.state, ""),
                "summary": v.tooltip, "counts": "", "record_note": "",
                "anti": [], "effects_heading": "", "effects": []}
    heading = ("Committed so far, each effect badged"
               if badge.state in (vb.HANDOFF, vb.FAILED) else "Recorded effects")
    return {
        "state": v.state, "show": True, "label": v.label, "icon": v.icon,
        "pill_class": v.pill_class, "title": _TITLE[v.state],
        "summary": v.tooltip, "counts": v.detail,
        "record_note": _record_note(badge),
        "anti": _anti_receipt(badge),
        "effects_heading": heading if badge.effects else "",
        "effects": [_effect_receipt(e) for e in badge.effects],
    }


# ── rendering (token classes only; ``_ui`` is a test seam) ────────────────────

def _the_ui(_ui):
    if _ui is None:
        from nicegui import ui as _ui
    return _ui


def _render_effect(eff: Dict[str, Any], _ui) -> None:
    with _ui.element("div").classes("s-card w-full q-mb-sm"):
        with _ui.row().classes("w-full items-center q-gutter-sm"):
            _ui.icon(eff["icon"])
            _ui.label(eff["label"]).classes(eff["pill_class"]).tooltip(eff["tooltip"])
            _ui.label(eff["objective"]).classes("s-cell s-cell--bold")
        for name, value in eff["rows"]:
            with _ui.row().classes("w-full q-gutter-sm"):
                _ui.label(name).classes("s-muted")
                _ui.label(value).classes("s-mono")
        lines = eff["note_lines"]
        if lines:
            head, rest = lines[:EXCERPT_LINES], lines[EXCERPT_LINES:]
            _ui.label("\n".join(head)).classes("s-mono")
            if rest:
                with _ui.expansion(f"Show {len(rest)} more line(s)").props("dense"):
                    _ui.label("\n".join(rest)).classes("s-mono")
        if eff["anti"]:
            _ui.label(eff["anti"]).classes("s-muted")


def render_receipt_panel(model: Dict[str, Any], *, _ui: Any = None) -> None:
    """Render a :func:`receipt_panel_model`."""
    _ui = _the_ui(_ui)
    if not model.get("title"):
        return
    with _ui.row().classes("w-full items-center q-gutter-sm"):
        _ui.icon(model["icon"])
        _ui.label(model["title"]).classes("s-section-head")
    _ui.label(model["summary"]).classes("s-muted")
    if model.get("counts"):
        _ui.label(model["counts"]).classes("s-mono")
    for line in model.get("anti") or []:
        _ui.label(line).classes("s-cell")
    if model.get("record_note"):
        _ui.label(model["record_note"]).classes("s-muted")
    if model.get("effects_heading"):
        _ui.label(model["effects_heading"]).classes("s-section-head q-mt-md")
    for eff in model.get("effects") or []:
        _render_effect(eff, _ui)


def open_receipt_dialog(badge: Any, *, _ui: Any = None):
    """One click from the badge: the receipt, from the value already minted."""
    _ui = _the_ui(_ui)
    model = receipt_panel_model(badge)
    with _ui.dialog() as dialog, _ui.card().classes("s-card"):
        render_receipt_panel(model, _ui=_ui)
        _ui.button("Close", on_click=dialog.close).classes("s-btn s-btn--ghost")
    dialog.open()
    return dialog


def render_badge(badge: Any, *, _ui: Any = None, show_counts: bool = False) -> None:
    """THE badge (PAR-2): the minted value's pill, as a button that opens its
    receipt. Renders nothing for an outcome not reached yet."""
    _ui = _the_ui(_ui)
    v = vb.view(badge)
    if not v.show:
        return
    _ui.button(v.label, icon=v.icon,
               on_click=lambda _=None, b=badge: open_receipt_dialog(b)) \
        .classes(v.pill_class).props("flat dense no-caps").tooltip(v.tooltip)
    if show_counts and v.detail:
        _ui.label(v.detail).classes("s-mono")


def render_committed_effects_list(badge: Any, *, _ui: Any = None) -> None:
    """The handoff state (IMPL-7) inline: what the run already committed, each
    effect with its own badge. Renders for a handoff or failed outcome only; an
    unreadable record says so instead of rendering an empty list."""
    _ui = _the_ui(_ui)
    if type(badge) is not vb.RunBadge or badge.state not in (vb.HANDOFF, vb.FAILED):
        return
    model = receipt_panel_model(badge)
    _ui.label("Committed so far").classes("s-section-head")
    if model["record_note"]:
        _ui.label(model["record_note"]).classes("s-muted")
    for eff in model["effects"]:
        with _ui.row().classes("w-full items-center q-gutter-sm"):
            _ui.label(eff["label"]).classes(eff["pill_class"]).tooltip(eff["tooltip"])
            _ui.label(eff["objective"]).classes("s-cell")
            if eff["note_lines"]:
                _ui.label(eff["note_lines"][0]).classes("s-muted")


def badge_model_for(status: Any, execution_id: Any) -> Optional[vb.RunBadge]:
    """Mint a run's badge for a surface row. Never raises: a mint that failed to
    run renders as not recorded through the fence (``None`` is not a badge)."""
    try:
        return vb.run_badge(status, execution_id)
    except Exception:
        return None
