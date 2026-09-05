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


#: Phase 2e - the routes that may carry an OPTIONAL per-page micro-tour
#: (``?ptour=N``). "/" is deliberately absent: Home belongs to the main tour,
#: and a second card there would compete with it.
PAGE_TOUR_ROUTES = ("/work", "/tools", "/table", "/shadows", "/insights")

#: The floating card chrome, shared by the main tour card and the page-tour
#: card so a micro-tour is visually the same object the operator already met.
_CARD_STYLE = ("position: fixed; bottom: 24px; right: 24px; z-index: 5000; "
               "max-width: 380px; box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);")


def tour_step(index: int) -> Optional[Dict[str, str]]:
    """Bounds-safe step lookup (None past either end)."""
    if 0 <= index < len(TOUR_STEPS):
        return TOUR_STEPS[index]
    return None


def tour_steps_for(persona: Optional[str]) -> List[Dict[str, str]]:
    """The six steps in this persona's order (default order when unknown).

    A skin re-emphasises, it never hides a room: an order that is not a clean
    run of in-range indices falls back to ``TOUR_STEPS`` whole rather than
    stranding a surface. Pure + deterministic, so ``?tour=N`` links stay
    stateless. Never raises.
    """
    try:
        from systemu.interface.persona_content import skin_for
        order = skin_for(persona).tour_order
        if type(order) is list and len(order) == len(TOUR_STEPS) and all(
                type(i) is int and 0 <= i < len(TOUR_STEPS) for i in order):
            return [TOUR_STEPS[i] for i in order]
    except Exception:
        logger.debug("[Tour] persona order lookup failed", exc_info=True)
    return list(TOUR_STEPS)


def _current_persona_name() -> Optional[str]:
    """The operator's stored persona, or None. Never raises.

    Defensive by construction: no app state, no vault, or a vault that throws
    all resolve to None, which every caller reads as "use the default skin".
    """
    try:
        from systemu.interface.dashboard_state import AppState
        from systemu.interface.persona_content import current_persona
        vault = AppState.get().vault
        if vault is None:
            return None
        return current_persona(vault)
    except Exception:
        logger.debug("[Tour] persona resolution failed", exc_info=True)
        return None


def _persona_steps() -> List[Dict[str, str]]:
    """Resolve the operator's persona ONCE per render, then order the steps.

    Defensive by construction: no app state, no vault, or a vault that throws
    all land on the default order: the tour must render regardless.
    """
    return tour_steps_for(_current_persona_name())


def _bare_route(route) -> str:
    """The path half of a route string ('' for anything that is not a str).

    The layout hands us its own page path, but a caller that passes the live
    URL must not be punished for the query string it is asking about.
    """
    if type(route) is not str:
        return ""
    return route.split("?", 1)[0]


def page_tour_steps(route, persona) -> List[Dict[str, str]]:
    """The 1-2 micro-tour hints for one route, in this persona's wording.

    Resolution order: the persona's own hints for the route, then
    ``DEFAULT_SKIN``'s, then ``[]``. A skin that omits a route INHERITS the
    default wording rather than losing the hint - emphasis is additive here,
    exactly as ``tour_steps_for`` refuses to let an order drop a room. Pure,
    bounds-safe and never raises: an unknown or hostile route is simply ``[]``,
    which every caller reads as "no pill, no card".
    """
    bare = _bare_route(route)
    if bare not in PAGE_TOUR_ROUTES:
        return []
    try:
        from systemu.interface import persona_content
        default_skin = persona_content.DEFAULT_SKIN
    except Exception:                                    # pragma: no cover
        logger.debug("[Tour] persona content unavailable", exc_info=True)
        return []
    candidates = []
    try:
        candidates.append(persona_content.skin_for(persona))
    except Exception:
        logger.debug("[Tour] persona skin lookup failed", exc_info=True)
    candidates.append(default_skin)
    for skin in candidates:
        hints = getattr(skin, "page_hints", None)
        if type(hints) is not dict:
            continue
        steps = hints.get(bare)
        if type(steps) is list and steps:
            return list(steps)                # a copy: callers cannot edit data
    return []


def page_tour_pill_model(route, persona) -> Dict[str, object]:
    """Pure: does this page offer a micro-tour, and where does the pill point?

    ``{"visible": bool, "target": str}``. Invisible is the honest default - no
    hints for this persona on this route means no pill at all, and Home never
    gets one because it is not a page-tour route. Never raises.
    """
    bare = _bare_route(route)
    if not page_tour_steps(bare, persona):
        return {"visible": False, "target": ""}
    return {"visible": True, "target": f"{bare}?ptour=0"}


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


def mark_tour_completed(vault, *, ended_early: bool = False,
                        note: str = "") -> None:
    """Record completion (the W11.3 ``tour_completed`` check reads this).

    ``note`` overrides the recorded wording so a caller that is not the browser
    card can say what actually happened — the headless
    ``sharing_on onboarding complete-tour`` records that the tour was waived on
    a machine with no browser rather than claiming the operator watched it.
    """
    from systemu.runtime.first_run import TOUR_FACT_TAG
    from systemu.runtime.user_profile import add_fact
    text = note.strip() or ("guided tour ended early by operator" if ended_early
                            else "guided tour completed")
    add_fact(vault, text, source="onboarding", tags=[TOUR_FACT_TAG, "onboarding"])


def parse_step_param(raw) -> Optional[int]:
    """Pure: a query-string step index, or None for anything that is not one.

    Bounds-safety at the boundary. Only a plain run of ASCII digits parses:
    negatives, floats, exponents, padding, non-ASCII digit forms and
    non-strings are all None, so a hand-edited URL can never index backwards
    into a step list or hand ``int()`` something it will choke on.
    """
    if type(raw) is not str:
        return None
    if not (raw.isascii() and raw.isdigit()):
        return None
    return int(raw)


def _query_step_index(name: str) -> Optional[int]:
    """The named step param of the current page request, else None.

    Never raises: outside a live request (tests, headless callers) there is no
    query string, so there is no index - which is what keeps both tours from
    firing on their own.
    """
    try:
        from nicegui import ui
        return parse_step_param(ui.context.client.request.query_params.get(name))
    except Exception:
        return None


def _active_step_index() -> Optional[int]:
    """The ?tour=N param of the current page request, else None. Never raises."""
    return _query_step_index("tour")


def _active_page_step_index() -> Optional[int]:
    """The ?ptour=N param of the current page request, else None. Never raises."""
    return _query_step_index("ptour")


def maybe_render_tour(current_path: str) -> None:
    """Render the floating tour card when ``?tour=N`` is active.

    Called from ``_build_layout`` on every page — the card floats over
    whatever route the active step navigated to. ``N`` indexes the
    persona-ordered list; out-of-range indices render nothing (stale links
    are harmless).
    """
    idx = _active_step_index()
    if idx is None:
        return
    steps = _persona_steps()
    if not 0 <= idx < len(steps):
        return
    render_tour_card(idx, steps=steps)


def maybe_render_page_tour(current_path: str) -> None:
    """Render the per-page hint card when ``?ptour=N`` is active.

    Called from ``_build_layout`` beside the main tour card. It NEVER auto-
    fires: with no ``?ptour`` in the query string this returns before it even
    looks a hint up, so an ordinary page load renders exactly what it rendered
    before this feature existed. Only the header "?" pill puts the param in the
    URL. Out-of-range indices render nothing - a stale link is harmless.
    """
    idx = _active_page_step_index()
    if idx is None:
        return
    try:
        persona = _current_persona_name()
    except Exception:                       # a hostile resolver is not a crash
        persona = None
    steps = page_tour_steps(current_path, persona)
    if not 0 <= idx < len(steps):
        return
    render_page_tour_card(idx, _bare_route(current_path), steps)


def render_tour_card(idx: int,
                     steps: Optional[List[Dict[str, str]]] = None) -> None:
    """The floating step card: progress, plain-language copy, Back/Next.

    ``steps`` is the persona-ordered list the caller already resolved; it is
    resolved here when omitted so the card stays independently callable.
    """
    from nicegui import ui
    from systemu.interface.dashboard_state import AppState
    from systemu.interface.design.primitives import button

    if steps is None:
        steps = _persona_steps()
    step = steps[idx]
    total = len(steps)

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

    with ui.element("div").classes("s-card").style(_CARD_STYLE):
        ui.label(f"Tour · step {idx + 1} of {total}").classes("s-muted").style(
            "font-size: 11px;")
        ui.label(step["title"]).classes("s-section-head")
        ui.label(step["body"]).classes("s-cell").style("white-space: normal;")
        with ui.row().classes("w-full q-gutter-sm").style("margin-top: 8px;"):
            if idx > 0:
                button("Back", variant="ghost",
                       on_click=lambda _=None, i=idx - 1: ui.navigate.to(
                           f"{steps[i]['route']}?tour={i}"))
            if idx + 1 < total:
                button("Next", variant="primary",
                       on_click=lambda _=None, i=idx + 1: ui.navigate.to(
                           f"{steps[i]['route']}?tour={i}"))
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


def render_page_tour_card(idx: int, route: str,
                          steps: List[Dict[str, str]]) -> None:
    """The floating hint card for the page the operator is already on.

    Same chrome as the main tour card (``_CARD_STYLE``), different KIND of
    object. A page tour is deliberately STATELESS: it records no completion
    fact and reads none. There is nothing to remember, so there is nothing to
    un-remember - it is a replayable throwaway the operator re-opens from the
    "?" pill whenever they want it, and "Done" simply drops the ``?ptour``
    param and leaves them standing on the page.
    """
    from nicegui import ui
    from systemu.interface.design.primitives import button

    step = steps[idx]
    total = len(steps)

    with ui.element("div").classes("s-card").style(_CARD_STYLE):
        ui.label(f"On this page - tip {idx + 1} of {total}").classes(
            "s-muted").style("font-size: 11px;")
        ui.label(step["title"]).classes("s-section-head")
        ui.label(step["body"]).classes("s-cell").style("white-space: normal;")
        with ui.row().classes("w-full q-gutter-sm").style("margin-top: 8px;"):
            if idx > 0:
                button("Back", variant="ghost",
                       on_click=lambda _=None, i=idx - 1: ui.navigate.to(
                           f"{route}?ptour={i}"))
            if idx + 1 < total:
                button("Next", variant="primary",
                       on_click=lambda _=None, i=idx + 1: ui.navigate.to(
                           f"{route}?ptour={i}"))
            # No completion fact on ANY exit path - see the docstring.
            button("Done", variant="ghost",
                   on_click=lambda _=None: ui.navigate.to(route))


def render_tour_pill(vault) -> None:
    """Header pill that keeps an unfinished tour visible until completed."""
    from nicegui import ui
    if vault is None or not is_tour_pending(vault):
        return
    ui.link("Take the tour", "/?tour=0").classes("s-pill s-pill--info").style(
        "text-decoration: none; cursor: pointer;")


def render_page_tour_pill(current_path: str) -> None:
    """Header "?" pill: this page has hints, and they are one click away.

    Renders ONLY where ``page_tour_pill_model`` says there is something to
    show, so a page with no hints for this operator's persona is untouched.
    Visibility is decided by that pure model, never re-derived here.
    """
    from nicegui import ui
    model = page_tour_pill_model(current_path, _current_persona_name())
    if not model["visible"]:
        return
    pill = ui.link("?", str(model["target"])).classes("s-pill").style(
        "text-decoration: none; cursor: pointer; font-weight: 700;")
    with pill:
        ui.tooltip("Quick tips for this page")
