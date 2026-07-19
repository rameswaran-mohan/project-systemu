"""UX-13 — the Ctrl+K command palette (R-UX3).

Keyboard-first search-and-go across the five spec groups (Actions, Tools, Runs,
Table, Asks), rendered on every page by the shared layout.

THE SAFETY LINE (spec hard rule, CI-asserted in tests/test_rux3_command_palette.py)
-----------------------------------------------------------------------------------
The palette **navigates and prefills only**. A ``PaletteEntry`` can carry no
intent other than ``navigate`` or ``prefill``, and the constructor refuses
anything else. Choosing a tool does not run it -- it lands you on the chat page
with "run: <tool>" typed into the box, so execution still enters the normal
lanes and passes every gate it would otherwise pass. There is deliberately no
code path here that resolves a decision, dispatches, or spawns anything.

Shape of the work (UX-9)
------------------------
``build_index(vault)`` derives the index ONCE per page render from in-memory
projections; ``match()`` is pure and takes no vault, so opening the overlay and
typing in it cannot read the vault. Matching is deterministic lexical scoring --
no model call on this or any other hot path.

Every projection is read defensively: one broken source degrades that group to
empty, it never blanks the palette.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

logger = logging.getLogger(__name__)

# The five groups, in render order (spec: Actions - Tools - Runs - Table - Asks).
GROUPS = ("Actions", "Tools", "Runs", "Table", "Asks")
_GROUP_ORDER = {g: i for i, g in enumerate(GROUPS)}

# The ONLY two intents that may exist. Adding a third is a safety decision, not
# a refactor -- the tests assert this set verbatim.
NAVIGATE = "navigate"
PREFILL = "prefill"
ALLOWED_INTENTS = frozenset({NAVIGATE, PREFILL})

_DEFAULT_LIMIT = 20
_PER_GROUP_CAP = 40          # keeps the per-session index compact


@dataclass(frozen=True)
class PaletteEntry:
    """One selectable row. ``target`` is always a local dashboard path."""

    group: str
    label: str
    intent: str
    target: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.intent not in ALLOWED_INTENTS:
            raise ValueError(
                f"palette intent {self.intent!r} is not allowed -- the palette "
                f"navigates and prefills only (allowed: {sorted(ALLOWED_INTENTS)})"
            )
        if self.group not in _GROUP_ORDER:
            raise ValueError(f"unknown palette group {self.group!r}")
        target = str(self.target)
        # "//host/path" is PROTOCOL-RELATIVE: it starts with "/" but the browser
        # resolves it off-box. A bare startswith("/") check let it through.
        if not target.startswith("/") or target.startswith("//"):
            raise ValueError(
                f"palette target {self.target!r} is not a local path")


# ── the static page / action registry ───────────────────────────────────────
# Kept literal (spec: "a static page/action registry"). test_rux3_command_palette
# asserts each path is a route the dashboard actually registers, so this cannot
# silently drift into dead links.
_STATIC_ACTIONS: Sequence[tuple] = (
    ("Home", "/", "the console"),
    ("Work -- runs and workflows", "/work", ""),
    ("Shadows", "/shadows", ""),
    ("Build -- tools, skills, evolutions", "/tools", ""),
    ("Insights", "/insights", ""),
    ("Settings", "/settings", ""),
    ("Privacy", "/privacy", "what leaves this machine"),
    ("Health -- is the daemon alive?", "/health", ""),
    ("Inbox -- things needing you", "/inbox", ""),
    ("The table -- what systemu can use", "/table", ""),
    ("Chat -- ask for something", "/chat", ""),
    ("Activities", "/activities", ""),
    ("Scrolls", "/scrolls", ""),
    ("Skills", "/skills", ""),
    ("Evolutions", "/evolutions", ""),
)


def _static_entries() -> List[PaletteEntry]:
    return [PaletteEntry(group="Actions", label=label, intent=NAVIGATE,
                         target=path, detail=detail)
            for label, path, detail in _STATIC_ACTIONS]


def prefill_target(text: str) -> str:
    """The chat route that lands with ``text`` typed into the box (not sent)."""
    return f"/chat?prefill={quote(text, safe='')}"


# ── the projections ─────────────────────────────────────────────────────────

def _tool_entry(name: str, detail: str) -> PaletteEntry:
    return PaletteEntry(
        group="Tools",
        label=name,
        # Selecting a tool NEVER runs it -- it types the request for you.
        intent=PREFILL,
        target=prefill_target(f"run: {name}"),
        detail=detail,
    )


def _tool_entries(vault) -> List[PaletteEntry]:
    """Tools from the R-CAP1 capability index, falling back to the plain tool
    index -- which is exactly the source order the spec names ("capability-index
    rows post-R-CAP1, else the tool index").

    The capability index is the PRIMARY source and works: ``derive_index``
    returns real rows for a live vault.

    That was not true when this was written. ``capability_index._ready()``
    required a truthy ``implementation_path`` which no ``_tool_header`` producer
    ever emitted, so the index was empty on every real vault -- and every test
    covering it built a fake vault that supplied the field, including the test
    written to close the adjacent dict-vs-object bug. This fallback was carrying
    the whole Tools group in production. Fixed in the same batch that landed this
    file; the header now emits the field and a realism pin, parametrized over both
    header producers, fails if a fixture supplies a key the real producer does not.

    The fallback stays anyway, because the spec names that source order and a
    vault whose tools genuinely cannot be indexed should still surface them
    rather than showing an operator an empty Tools group. It is pinned against a
    deliberately-emptied index, not against a defect.
    """
    out: List[PaletteEntry] = []
    try:
        from systemu.runtime import capability_index
        for row in capability_index.derive_index(vault)[:_PER_GROUP_CAP]:
            name = getattr(row, "name", "") or getattr(row, "tool_id", "")
            if not name:
                continue
            slots = getattr(row, "slots", None) or []
            out.append(_tool_entry(name, ", ".join(str(s) for s in slots)
                                   or (getattr(row, "detail", "") or "")))
    except Exception:
        logger.debug("[Palette] capability index unavailable", exc_info=True)

    if out:
        return out

    for row in list(vault.list_tools() or [])[:_PER_GROUP_CAP]:
        name = (row.get("name") if isinstance(row, dict)
                else getattr(row, "name", "")) or ""
        if not name:
            continue
        if isinstance(row, dict):
            if not row.get("enabled", False):
                continue
            detail = str(row.get("description", "") or "")[:120]
        else:
            if not getattr(row, "enabled", False):
                continue
            detail = str(getattr(row, "description", "") or "")[:120]
        out.append(_tool_entry(str(name), detail))
    return out


def _run_entries(vault) -> List[PaletteEntry]:
    """Recent activities. There is no per-activity detail route today, so these
    land on the activities list -- honest navigation rather than a fake link."""
    rows = vault.load_index("activities") or []
    out: List[PaletteEntry] = []
    for row in list(rows)[:_PER_GROUP_CAP]:
        if not isinstance(row, dict):
            continue
        name = row.get("name") or row.get("id") or ""
        if not name:
            continue
        out.append(PaletteEntry(
            group="Runs", label=str(name), intent=NAVIGATE,
            target="/activities", detail=str(row.get("status", "") or ""),
        ))
    return out


def _table_entries(vault) -> List[PaletteEntry]:
    from systemu.runtime import table_reconciler

    out: List[PaletteEntry] = []
    for item in table_reconciler.project(vault)[:_PER_GROUP_CAP]:
        name = getattr(item, "name", "") or ""
        if not name:
            continue
        out.append(PaletteEntry(
            group="Table", label=name, intent=NAVIGATE, target="/table",
            detail=str(getattr(item, "kind", "") or ""),
        ))
    return out


def _ask_entries(vault) -> List[PaletteEntry]:
    """Open asks AND gates -- both are things waiting on the operator.

    Selecting one opens its card on /inbox; it never resolves anything.
    """
    out: List[PaletteEntry] = []

    from systemu.interface.components.attention import pending_ask_rows
    for row in pending_ask_rows(vault)[:_PER_GROUP_CAP]:
        title = row.get("title") or row.get("id") or ""
        if title:
            out.append(PaletteEntry(
                group="Asks", label=str(title), intent=NAVIGATE,
                target="/inbox", detail=str(row.get("kind", "") or "ask"),
            ))

    # Gates are owned by InboxQueue, not the ask projection -- read the
    # descriptors through the same read-only listing the Inbox page uses.
    try:
        from systemu.interface.command import inbox as _inbox
        for _dec_id, desc in _inbox.InboxQueue(vault).list_descriptors():
            title = getattr(desc, "title", "") or ""
            if title:
                out.append(PaletteEntry(
                    group="Asks", label=str(title), intent=NAVIGATE,
                    target="/inbox",
                    detail=str(getattr(desc, "risk", "") or "gate"),
                ))
    except Exception:
        logger.debug("[Palette] gate descriptors unavailable", exc_info=True)

    return out


_SOURCES = (
    ("Tools", _tool_entries),
    ("Runs", _run_entries),
    ("Table", _table_entries),
    ("Asks", _ask_entries),
)


def build_index(vault) -> List[PaletteEntry]:
    """Derive the per-session palette index from in-memory projections.

    Defensive by construction: the static actions always render, and each
    vault-backed source is isolated so one failure cannot blank the palette.
    """
    entries: List[PaletteEntry] = _static_entries()
    if vault is None:
        return entries
    for name, fn in _SOURCES:
        try:
            entries.extend(fn(vault))
        except Exception:
            logger.debug("[Palette] %s projection unavailable", name,
                         exc_info=True)
    return entries


# ── deterministic lexical matching (never a model call) ─────────────────────

def _score(entry: PaletteEntry, needle: str) -> Optional[int]:
    """Lower is better; ``None`` means no match.

    Ranking, best to worst: exact label, label prefix, word-start inside the
    label, substring in the label, substring in the detail, subsequence in the
    label (the loose "fuzzy" tail).
    """
    label = entry.label.lower()
    detail = (entry.detail or "").lower()

    if label == needle:
        return 0
    if label.startswith(needle):
        return 1
    if any(w.startswith(needle) for w in label.replace("-", " ").split()):
        return 2
    pos = label.find(needle)
    if pos >= 0:
        return 3 + min(pos, 50)
    if needle in detail:
        return 60
    # subsequence: every character of the needle appears in order
    it = iter(label)
    if all(ch in it for ch in needle):
        return 80
    return None


def match(entries: Sequence[PaletteEntry], query: str, *,
          limit: int = _DEFAULT_LIMIT) -> List[PaletteEntry]:
    """Rank ``entries`` against ``query``. Pure -- takes no vault, so opening
    the palette and typing in it performs no I/O at all.

    Deterministic: ties break on (group order, label, target), never on dict or
    set iteration order.
    """
    needle = (query or "").strip().lower()
    if not needle:
        ordered = sorted(
            entries,
            key=lambda e: (_GROUP_ORDER.get(e.group, 99), e.label.lower(), e.target),
        )
        return list(ordered[:max(0, int(limit))])

    scored = []
    for e in entries:
        s = _score(e, needle)
        if s is not None:
            scored.append((s, _GROUP_ORDER.get(e.group, 99), e.label.lower(),
                           e.target, e))
    scored.sort(key=lambda t: t[:4])
    return [t[4] for t in scored[:max(0, int(limit))]]


def grouped(results: Sequence[PaletteEntry]) -> List[tuple]:
    """``[(group, [entry, ...]), ...]`` in spec order, empty groups dropped."""
    buckets: Dict[str, List[PaletteEntry]] = {g: [] for g in GROUPS}
    for e in results:
        buckets.setdefault(e.group, []).append(e)
    return [(g, buckets[g]) for g in GROUPS if buckets.get(g)]


# ── the overlay (NiceGUI) ───────────────────────────────────────────────────

def build_command_palette(vault) -> None:
    """Mount the Ctrl+K overlay on the current page.

    Called once from the shared layout, so the palette exists on every page.
    Rendering failures are swallowed: a broken palette must never take the
    page down with it.
    """
    try:
        from nicegui import ui
    except Exception:                                    # pragma: no cover
        return

    try:
        entries = build_index(vault)
    except Exception:                                    # pragma: no cover
        logger.debug("[Palette] index build failed", exc_info=True)
        return

    with ui.dialog() as dialog, ui.card().classes("s-card").style(
        "min-width: 520px; max-width: 90vw;"
    ):
        box = ui.input(placeholder="Search pages, tools, runs, table, asks...") \
            .classes("s-input").props("autofocus outlined dense")
        results = ui.column().classes("s-palette-results").style(
            "width: 100%; gap: 2px; max-height: 50vh; overflow-y: auto;")
        ui.label(
            "Enter opens the highlighted item. Tools are typed into chat for "
            "you -- the palette never runs anything."
        ).classes("s-muted").style("font-size: 11px; margin-top: 8px;")

    def _go(entry: PaletteEntry) -> None:
        # navigate/prefill ONLY -- both are a URL change, nothing else.
        dialog.close()
        ui.navigate.to(entry.target)

    def _render(_e=None) -> None:
        results.clear()
        with results:
            hits = match(entries, box.value or "")
            if not hits:
                ui.label("No matches").classes("s-muted").style(
                    "font-size: 12px; padding: 6px;")
                return
            for group, rows in grouped(hits):
                ui.label(group.upper()).classes("s-field-label")
                for entry in rows:
                    text = entry.label
                    if entry.detail:
                        text += f"  --  {entry.detail}"
                    ui.button(text, on_click=lambda _e=None, x=entry: _go(x)) \
                        .props("flat dense align=left no-caps") \
                        .classes("s-palette-row").style("width: 100%;")

    box.on("update:model-value", _render)
    _render()

    def _open() -> None:
        box.value = ""
        _render()
        dialog.open()

    def _on_key(e) -> None:
        try:
            action = getattr(e, "action", None)
            if not getattr(action, "keydown", False):
                return
            key = getattr(e, "key", None)
            mods = getattr(e, "modifiers", None)
            if str(key) == "k" and (getattr(mods, "ctrl", False)
                                    or getattr(mods, "meta", False)):
                _open()
        except Exception:                                # pragma: no cover
            logger.debug("[Palette] key handler failed", exc_info=True)

    # ignore=[] deliberately. NiceGUI's default is
    # ignore=['input','select','button','textarea'], which would swallow the
    # shortcut exactly where an operator is most likely to press it (mid-typing
    # in the chat box) -- i.e. the palette would appear not to work. The handler
    # above returns immediately unless the chord is ctrl/meta+k, so ordinary
    # typing is unaffected.
    #
    # UNVERIFIED: the binding itself is a browser-side interaction and is not
    # covered by any test here (see the test module's docstring).
    ui.keyboard(on_key=_on_key, ignore=[])
