"""W11.5 — the guided tour: mandatory on first run, replayable forever.

The wizard's Finish lands on ``/?tour=0``; a floating card walks the spine
surfaces in plain language, navigating route to route via ``?tour=N``. The
tour never causes redirects (it IS navigation) — an unfinished tour stays
visible as a header "Take the tour" pill until completed. "End tour" also
records completion (mandatory must never mean hostage); Settings offers a
replay any time.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

TOUR_STEPS: List[Dict[str, str]] = [
    {
        "route": "/",
        "title": "Home — your desk at a glance",
        "body": ("What needs you, what's running, and what happened "
                 "recently — every item links straight to where you act "
                 "on it."),
    },
    {
        "route": "/chat",
        "title": "Chat — where work starts",
        "body": ("Type any task in plain English. Quick answer handles "
                 "one-shot asks in seconds; Workflow mode teaches Systemu "
                 "a repeatable job. You can also hit ＋New → Record session "
                 "and just DO a task once — Systemu watches and learns it. "
                 "That's the superpower."),
    },
    {
        "route": "/work",
        "title": "Work — the workflows it has learned",
        "body": ("Every captured or submitted task becomes a workflow here, "
                 "with its stages and live status. Run them again any time, "
                 "or approve the ones waiting for your go-ahead."),
    },
    {
        "route": "/inbox",
        "title": "Inbox — you stay in control",
        "body": ("Whenever Systemu needs a yes — installing something, "
                 "running newly written code, a risky step — the question "
                 "lands here and on the Needs-you badge above. Nothing "
                 "sensitive happens without you."),
    },
    {
        "route": "/tools",
        "title": "Build — its toolbox",
        "body": ("The tools Systemu can use: web search, file writing, and "
                 "everything it forges for itself. Each one stays OFF until "
                 "you enable it — you decide what it may touch."),
    },
    {
        "route": "/settings",
        "title": "Settings — models, connections, trust",
        "body": ("Pick the model preset (its brain), connect MCP servers or "
                 "Telegram, and tune how much it may do on its own. You can "
                 "replay this tour from here whenever you like."),
    },
]


def tour_step(index: int) -> Optional[Dict[str, str]]:
    """Bounds-safe step lookup (None past either end)."""
    if 0 <= index < len(TOUR_STEPS):
        return TOUR_STEPS[index]
    return None


def is_tour_pending(vault) -> bool:
    """True when the wizard is done but the tour never finished.

    Drives the header "Take the tour" pill. Pre-wizard installs return
    False — funneling them is the W11.4 gate's job, not the pill's. Honors
    the SYSTEMU_SKIP_ONBOARDING escape hatch. Never raises.
    """
    import os
    try:
        if (os.environ.get("SYSTEMU_SKIP_ONBOARDING", "") or "").lower() in ("1", "true"):
            return False
        from systemu.runtime.first_run import tour_completed
        if tour_completed(vault):
            return False
        return vault.get_user_profile() is not None
    except Exception:
        return False


def mark_tour_completed(vault, *, ended_early: bool = False) -> None:
    """Record completion (the W11.3 ``tour_completed`` check reads this)."""
    from systemu.runtime.first_run import TOUR_FACT_TAG
    from systemu.runtime.user_profile import add_fact
    note = ("guided tour ended early by operator" if ended_early
            else "guided tour completed")
    add_fact(vault, note, source="onboarding", tags=[TOUR_FACT_TAG, "onboarding"])


def _active_step_index() -> Optional[int]:
    """The ?tour=N param of the current page request, else None. Never raises."""
    try:
        from nicegui import ui
        raw = ui.context.client.request.query_params.get("tour")
        return int(raw) if raw is not None and str(raw).isdigit() else None
    except Exception:
        return None


def maybe_render_tour(current_path: str) -> None:
    """Render the floating tour card when ``?tour=N`` is active.

    Called from ``_build_layout`` on every page — the card floats over
    whatever route the active step navigated to. Out-of-range indices
    render nothing (stale links are harmless).
    """
    idx = _active_step_index()
    if idx is None or tour_step(idx) is None:
        return
    render_tour_card(idx)


def render_tour_card(idx: int) -> None:
    """The floating step card: progress, plain-language copy, Back/Next."""
    from nicegui import ui
    from systemu.interface.dashboard_state import AppState
    from systemu.interface.design.primitives import button

    step = TOUR_STEPS[idx]
    total = len(TOUR_STEPS)

    def _vault():
        try:
            return AppState.get().vault
        except Exception:
            return None

    def _complete(ended_early: bool) -> None:
        v = _vault()
        if v is not None:
            try:
                mark_tour_completed(v, ended_early=ended_early)
            except Exception:
                logger.debug("[Tour] completion fact failed", exc_info=True)

    with ui.element("div").classes("s-card").style(
        "position: fixed; bottom: 24px; right: 24px; z-index: 5000; "
        "max-width: 380px; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);"
    ):
        ui.label(f"Tour · step {idx + 1} of {total}").classes("s-muted").style(
            "font-size: 11px;")
        ui.label(step["title"]).classes("s-section-head")
        ui.label(step["body"]).classes("s-cell").style("white-space: normal;")
        with ui.row().classes("w-full q-gutter-sm").style("margin-top: 8px;"):
            if idx > 0:
                button("Back", variant="ghost",
                       on_click=lambda _=None, i=idx - 1: ui.navigate.to(
                           f"{TOUR_STEPS[i]['route']}?tour={i}"))
            if idx + 1 < total:
                button("Next", variant="primary",
                       on_click=lambda _=None, i=idx + 1: ui.navigate.to(
                           f"{TOUR_STEPS[i]['route']}?tour={i}"))
            else:
                def _finish(_=None) -> None:
                    _complete(False)
                    ui.notify("Tour complete — it's all yours.", type="positive")
                    ui.navigate.to("/")

                button("Finish", variant="primary", on_click=_finish)

            def _end(_=None) -> None:
                # Ending early still completes — replay lives in Settings.
                _complete(True)
                ui.notify("Tour ended — replay any time from Settings.",
                          type="info")
                ui.navigate.to(step["route"])

            button("End tour", variant="ghost", on_click=_end)


def render_tour_pill(vault) -> None:
    """Header pill that keeps an unfinished tour visible until completed."""
    from nicegui import ui
    if vault is None or not is_tour_pending(vault):
        return
    ui.link("Take the tour", "/?tour=0").classes("s-pill s-pill--info").style(
        "text-decoration: none; cursor: pointer;")
