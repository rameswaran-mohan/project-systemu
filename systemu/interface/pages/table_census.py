"""The census consent panel on ``/table`` -- R-W2 / WM-7 ("census consents render on
the table"; spec 5.11 ownership rule: the table KEEPS the operator-facing surfaces).

WHAT IT SHOWS
-------------
Every declared census category, one row each: its STATE (granted / paused / not granted /
unavailable), the REASON whenever it is unavailable, what it READS and what it never
reads, how it reads it, when it last scanned and what it holds. All of it comes from
``ambient_census.census_overview``, which reads the consent file through the SAME
authenticated, fail-closed load the scanner gates on -- so this panel can never show
"granted" for a file the census itself refuses, and a refused file is SAID, not hidden.

WHAT IT CHANGES -- AND WHAT IT NEVER DOES
-----------------------------------------
Withdraw, Pause and Resume, one click each, per granted category: the same verbs as
`systemu census revoke`, `systemu census pause` and `systemu census resume`, through the
same library calls. They live in
the declared region at the END of this file.

GRANT IS CLI-ONLY (ruled). A grant is a standing permission to read this machine whose
findings go to the model provider, so it is taken where the roots grant is taken: a
terminal, with the full disclosure printed and a typed y/N (and a refusal when there is no
terminal). A Grant button here would be reachable by anything that can drive this page. A
not-granted row therefore shows the exact command, ``systemu census grant <category>``, as
copyable text -- and nothing reachable from ``interface/pages/`` may call the grant
(pinned by ``tests/test_p19_census_grant_is_cli_only.py``). Withdrawing is the safe
direction, which is why it is one click.

RESUME IS NOT A GRANT. It un-pauses a category that is still granted; for a withdrawn
category there is no row to un-pause and it records nothing (pinned by test).

DEC-27 -- a never-scanned category is never shown with a count, and "unavailable" always
carries its reason. The counts and their wording are decided in ``census_overview`` and
rendered here verbatim.

NO BLOCKING I/O ON THE UI THREAD (R-UX2). The overview reads two files and derives HMAC
keys, so the panel renders a placeholder and fetches it through ``run.io_bound`` from a
one-shot timer; every control runs its change through ``run.io_bound`` too.

ASCII only: every string below reaches the operator's screen.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from nicegui import run, ui

from systemu.runtime.ambient_census import (
    STATE_GRANTED, STATE_NOT_GRANTED, STATE_PAUSED, STATE_UNAVAILABLE, census_overview,
)

CENSUS_PANEL_TITLE = "What systemu may look at on this machine"
CENSUS_PANEL_BLURB = (
    "The census is off until you turn a category on. A grant is a STANDING permission: "
    "systemu re-checks that category on later runs until you withdraw it, and what it "
    "finds is sent to your model provider as part of planning. Withdrawing stops future "
    "scans and deletes what that category found.")
CENSUS_PANEL_LOADING = "Reading what the census may look at..."
CENSUS_PANEL_FAILED = ("Could not read the census state, so nothing is shown here rather "
                       "than a guess. `systemu census status` reads the same state.")
CENSUS_FACTS_UNREADABLE = ("The fact store could not be read, so what each category has "
                           "found is unknown.")
CENSUS_GRANT_LEAD = ("Turn it on from a terminal - it shows the full disclosure, then "
                     "asks you y/N:")
CENSUS_COMMAND_COPIED = "Command copied."
CENSUS_COMMAND_NOT_COPIED = ("Could not copy - the browser refused clipboard access. "
                             "Select the command text and copy it by hand.")

#: State -> the label an operator reads. Anything outside the four states renders as
#: "Unknown" -- never as one of the reassuring labels.
CENSUS_STATE_LABELS: Dict[str, str] = {
    STATE_GRANTED: "Granted",
    STATE_PAUSED: "Paused",
    STATE_NOT_GRANTED: "Not granted",
    STATE_UNAVAILABLE: "Unavailable",
}
CENSUS_STATE_UNKNOWN = "Unknown"

#: State -> Quasar colour token (no raw hex; the UI-style lint).
_STATE_COLOR: Dict[str, str] = {
    STATE_GRANTED: "positive", STATE_PAUSED: "warning",
    STATE_NOT_GRANTED: "grey", STATE_UNAVAILABLE: "grey",
}


# ── pure helpers (unit-tested; the render below only arranges their output) ────

def state_label(row: Dict[str, Any]) -> str:
    """The badge text for a row's state."""
    return CENSUS_STATE_LABELS.get(str((row or {}).get("state") or ""),
                                   CENSUS_STATE_UNKNOWN)


def reads_line(row: Dict[str, Any]) -> str:
    """"Reads: ..." -- the card's own ``collects``, verbatim."""
    items = [str(x) for x in ((row or {}).get("collects") or [])]
    return "Reads: " + ("; ".join(items) if items else "(not stated)")


def never_reads_line(row: Dict[str, Any]) -> str:
    """"Never reads: ..." -- the card's own ``excludes``, verbatim."""
    items = [str(x) for x in ((row or {}).get("excludes") or [])]
    return "Never reads: " + (", ".join(items) if items else "(not stated)")


def reason_line(row: Dict[str, Any]) -> str:
    """"Why: ..." for an unavailable row; ``""`` otherwise. An unavailable row with no
    stated reason says so rather than rendering as a bare badge (DEC-27)."""
    if str((row or {}).get("state") or "") != STATE_UNAVAILABLE:
        return ""
    reason = str((row or {}).get("reason") or "").strip()
    return "Why: " + (reason if reason else "no reason was recorded")


def grant_command(row: Dict[str, Any]) -> str:
    """The exact command that turns a not-granted category on, or ``""``.

    Only for a category the CLI really grants (``controls_shipped``) and that is
    ``not_granted`` -- an unavailable category never reaches that state, so a command the
    CLI would refuse is never offered.
    """
    r = row or {}
    if not bool(r.get("controls_shipped")) or str(r.get("state") or "") != STATE_NOT_GRANTED:
        return ""
    return f"systemu census grant {str(r.get('category') or '')}"


def control_lines(row: Dict[str, Any]) -> List[str]:
    """The commands that change this category -- ONLY where the CLI really offers them.

    Used by the static render (no live controls). A row whose category has no shipped
    control gets no command at all: naming ``census grant`` for a category the CLI refuses
    would be a promise the command breaks.
    """
    r = row or {}
    if not bool(r.get("controls_shipped")):
        return []
    cat = str(r.get("category") or "")
    state = str(r.get("state") or "")
    if state == STATE_GRANTED:
        return [f"Pause (keeps what it found): systemu census pause {cat}",
                f"Withdraw (deletes what it found): systemu census revoke {cat}"]
    if state == STATE_PAUSED:
        return [f"Resume: systemu census resume {cat}",
                f"Withdraw (deletes what it found): systemu census revoke {cat}"]
    if state == STATE_NOT_GRANTED:
        return [f"Grant (shows the full disclosure first): {grant_command(r)}"]
    return []


def row_actions(row: Dict[str, Any]) -> List[str]:
    """Which one-click buttons a row gets: a subset of pause / resume / withdraw.

    NEVER grant -- see the module docstring. Pause plus withdraw for a granted category,
    resume plus withdraw for a paused one, withdraw alone for a grant on a category this
    machine cannot scan (withdrawing is always the safe direction)."""
    r = row or {}
    state = str(r.get("state") or "")
    if not bool(r.get("controls_shipped")):
        return []
    if state == STATE_GRANTED:
        return ["pause", "withdraw"]
    if state == STATE_PAUSED:
        return ["resume", "withdraw"]
    if state == STATE_UNAVAILABLE and r.get("granted_at"):
        return ["withdraw"]
    return []


_ACTION_LABELS = {"pause": "Pause", "resume": "Resume",
                  "withdraw": "Withdraw (deletes what it found)"}

#: Copies, and RESOLVES to whether the browser accepted it -- the same rule as the folder
#: card in table.py: "copied" is said only when the browser says so (DEC-27).
_COPY_JS = ("(navigator.clipboard ? navigator.clipboard.writeText({text})"
            ".then(() => true, () => false) : false)")


async def _copy_command(cmd: str) -> None:
    try:
        copied = await ui.run_javascript(_COPY_JS.format(text=json.dumps(cmd)),
                                         timeout=3.0)
    except Exception:
        copied = False
    if copied is True:
        ui.notify(CENSUS_COMMAND_COPIED, type="positive")
    else:
        ui.notify(CENSUS_COMMAND_NOT_COPIED, type="warning", multi_line=True)


# ── render ─────────────────────────────────────────────────────────────────────

def _render_grant_command(cmd: str) -> None:
    """The CLI grant command as selectable, copyable text -- the panel's ONLY way on."""
    ui.label(CENSUS_GRANT_LEAD).classes("s-muted").style("font-size: 12px;")
    with ui.row().classes("items-center no-wrap w-full").style("gap: 4px;"):
        ui.label(cmd).classes("s-mono") \
            .style("font-size: 12px; user-select: all; word-break: break-all;")
        ui.button(icon="content_copy",
                  on_click=lambda _e=None, c=cmd: _copy_command(c)) \
            .props("flat dense round size=sm color=grey").tooltip("Copy command")


def _render_overview(overview: Optional[Dict[str, Any]],
                     actions: Optional[Dict[str, Any]] = None) -> None:
    """Arrange one overview. Reads no file: everything arrives in ``overview``. With
    ``actions`` (the live page) a granted row gets its one-click buttons and a
    not-granted row its copyable grant command; without (a static render) every row names
    its commands instead."""
    if not isinstance(overview, dict):
        ui.label(CENSUS_PANEL_FAILED).classes("s-text-warn").style("font-size: 12px;")
        return
    refused = str(overview.get("refused") or "")
    if refused:
        ui.label(refused).classes("s-text-warn").style("font-size: 12px;")
    if overview.get("facts_readable") is False:
        ui.label(CENSUS_FACTS_UNREADABLE).classes("s-text-warn").style("font-size: 12px;")
    for row in overview.get("rows") or []:
        if not isinstance(row, dict):
            continue
        color = _STATE_COLOR.get(str(row.get("state") or ""), "grey")
        with ui.column().classes("w-full q-pa-xs").style("gap: 2px;"):
            with ui.row().classes("items-center no-wrap w-full justify-between") \
                    .style("gap: 8px;"):
                ui.label(f"{row.get('title', '')} ({row.get('category', '')})") \
                    .classes("text-weight-medium ellipsis")
                ui.badge(state_label(row)).props(f"outline color={color}")
            _why = reason_line(row)
            if _why:
                ui.label(_why).classes("s-muted").style("font-size: 12px;")
            ui.label(reads_line(row)).style("font-size: 12px;")
            ui.label(never_reads_line(row)).classes("s-muted").style("font-size: 12px;")
            ui.label("How: " + str(row.get("how") or "")).classes("s-muted") \
                .style("font-size: 12px;")
            if row.get("sensitivity_notice"):
                ui.label("Note: " + str(row["sensitivity_notice"])).classes("s-text-warn") \
                    .style("font-size: 12px;")
            if row.get("facts_line"):
                ui.label(str(row["facts_line"])).style("font-size: 12px;")
            if actions is None:
                for line in control_lines(row):
                    ui.label(line).classes("s-muted").style("font-size: 12px;")
                continue
            _cmd = grant_command(row)
            if _cmd:
                _render_grant_command(_cmd)
            _verbs = row_actions(row)
            if _verbs:
                with ui.row().classes("items-center").style("gap: 6px;"):
                    for verb in _verbs:
                        ui.button(
                            _ACTION_LABELS[verb],
                            on_click=lambda _e=None, v=verb, r=row: actions[v](r),
                        ).props("dense flat color=primary" if verb != "withdraw"
                                else "dense flat color=negative")
        ui.separator()


def build_census_panel(vault) -> None:
    """Mount the panel: a placeholder now, the overview once ``run.io_bound`` returns."""
    view: Dict[str, Any] = {"loaded": False, "overview": None, "actions": None}

    @ui.refreshable
    def _census_body() -> None:
        if not view["loaded"]:
            ui.label(CENSUS_PANEL_LOADING).classes("s-muted").style("font-size: 12px;")
            return
        _render_overview(view["overview"], view["actions"])

    async def _census_load() -> None:
        try:
            view["overview"] = (await run.io_bound(census_overview, vault)
                                if vault is not None else None)
        except Exception:
            view["overview"] = None
        view["loaded"] = True
        _census_body.refresh()

    view["actions"] = (_census_panel_controls(vault, _census_load)
                       if vault is not None else None)

    with ui.column().classes("s-card q-pa-sm q-mt-md w-full").style("gap: 6px;"):
        ui.label(CENSUS_PANEL_TITLE).classes("s-section-head")
        ui.label(CENSUS_PANEL_BLURB).classes("s-muted").style("font-size: 12px;")
        _census_body()
    ui.timer(0.05, _census_load, once=True)


# ===========================================================================
# R-W2 CENSUS CONSENT SURFACE :: REGION START
# ===========================================================================
# The /table census panel's Withdraw / Pause / Resume handlers (P19). GRANT STAYS
# CLI-ONLY: nothing in this region (or anywhere reachable from interface/pages/) may call
# the grant -- tests/test_p19_census_grant_is_cli_only.py. The region guard in
# tests/test_rw2_ambient_census.py confines the consent symbols to this block, and
# tests/test_p19_census_consent_writer_set.py pins each reference by (file, function).
#
# The grant-surface guard's five re-audits, for this surface:
#   1. No grant here, so no consent card is rendered as an offer; the not-granted row
#      points at `systemu census grant`, which prints the full card first.
#   2. Withdraw and Pause/Resume ship together, for every surfaced category.
#   3. No second writer: every change goes through ambient_census's verbs, which reach
#      the store's signed write under its lock. This file never constructs a store.
#   4. The SCOPE prose in ambient_census and the reach section of the consent module
#      name this panel and its limits.
#   5. `systemu world` and `systemu census status` name this panel too.
# Resume un-pauses a still-granted category only: for a withdrawn one the store has no
# row to change and records nothing.
#
# Each change runs through run.io_bound -- the consent write derives an HMAC key and
# replaces a file, and Withdraw purges the fact store.


def _census_panel_controls(vault, reload) -> Dict[str, Any]:
    """Withdraw / Pause / Resume, one click each, bound to this vault and this panel."""
    from systemu.runtime.ambient_census import pause_category, revoke_category

    async def _withdraw(category: str) -> None:
        try:
            out = await run.io_bound(revoke_category, vault, category)
        except Exception as exc:
            ui.notify(f"Could not withdraw '{category}': {exc}", type="negative")
            return
        removed = int((out or {}).get("facts_removed") or 0)
        if (out or {}).get("revoked"):
            ui.notify(f"Withdrawn: '{category}' will not be scanned again. Facts "
                      f"deleted: {removed}.", type="positive")
        else:
            ui.notify(f"'{category}' was not granted. Facts deleted: {removed}.",
                      type="info")
        await reload()

    async def _pause_or_resume(category: str, paused: bool) -> None:
        try:
            ok = await run.io_bound(pause_category, vault, category, paused)
        except Exception:
            ok = False
        if not ok:
            ui.notify(f"'{category}' is not granted, so there is nothing to "
                      f"{'pause' if paused else 'resume'}.", type="warning")
        elif paused:
            ui.notify(f"Paused: '{category}' will not be scanned until you resume it. "
                      "What it found is kept.", type="info")
        else:
            ui.notify(f"Resumed: '{category}' will be scanned again on the next run.",
                      type="positive")
        await reload()

    def _category(row: Dict[str, Any]) -> str:
        return str((row or {}).get("category") or "")

    return {
        "withdraw": lambda row: _withdraw(_category(row)),
        "pause": lambda row: _pause_or_resume(_category(row), True),
        "resume": lambda row: _pause_or_resume(_category(row), False),
    }

# R-W2 CENSUS CONSENT SURFACE :: REGION END
