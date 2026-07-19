"""R-B4 — the source-level pins.

Split out of ``test_rb4_writeback_surfaces.py`` on purpose. These read source text,
so they are edit-race-prone in exactly the way conftest's ``source_sensitive``
auto-tag exists to isolate — but they use ``Path.read_text`` rather than
``inspect.getsource``, so the auto-tagger (which keys on the literal ``getsource(``)
would not catch them. The marker is therefore applied explicitly, and living in
their own module means they drag only themselves out of the edit-safe tier.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.source_sensitive

_RUNTIME = Path(__file__).resolve().parents[1] / "systemu" / "runtime"
_INTERFACE = Path(__file__).resolve().parents[1] / "systemu" / "interface"


def _callers_of(root: Path, needle: str, *, exclude: set) -> list:
    hits = []
    for path in root.rglob("*.py"):
        if path.name in exclude:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if needle in line and not stripped.startswith("#"):
                hits.append(f"{path.name}:{i}")
    return hits


def test_nothing_in_the_runtime_accepts_a_suggestion():
    """The §5.10.b#1 hard invariant, enforced structurally rather than by review.

    ``suggested`` must NEVER auto-promote. The whole guarantee reduces to one
    property: ``add_accepted`` is reached ONLY by a direct operator UI action. If a
    runtime module ever calls it — a reconciler "helpfully" accepting high-confidence
    suggestions, a task tool, a migration — the invariant is gone, and no behavioural
    test would catch it because each such call would look locally reasonable.

    ``table_store.py`` is excluded: that is where the function is DEFINED.
    """
    callers = _callers_of(_RUNTIME, "add_accepted(", exclude={"table_store.py"})
    assert callers == [], (
        "add_accepted() must be called only from the operator UI — found runtime "
        f"call sites: {callers}"
    )


def test_the_only_acceptance_writer_is_the_table_page_dialog():
    """And on the UI side it is reached from exactly one place: the accept dialog's
    commit, which always renders the §5.10.b#4 provenance banner first. Any second
    UI call site is an accept path that skips the banner."""
    callers = _callers_of(_INTERFACE, "add_accepted(", exclude=set())
    assert len(callers) == 1, (
        "expected exactly one UI acceptance writer (the /table accept dialog); "
        f"found: {callers}"
    )
    assert callers[0].startswith("table.py"), callers


def test_the_provenance_banner_has_no_defaulting_lookup():
    """The banner must not acquire a ``.get(prov, <something>)`` fallback.

    A default argument on the provenance→source lookup is precisely the flattering
    fallback the module exists to prevent: it would turn every unrecognised value
    into whatever the default names, silently. The membership test against
    ``ITEM_PROVENANCES`` is the intended shape, and it is only load-bearing while
    no lookup can bypass it.
    """
    text = (_RUNTIME / "table_provenance.py").read_text(encoding="utf-8")
    assert "_SOURCE.get(" not in text, \
        "_SOURCE must be indexed only after the membership test, never with a default"
    assert "_HEADLINE.get(" not in text, \
        "_HEADLINE must be indexed only after the membership test, never with a default"
    assert "ITEM_PROVENANCES" in text, "the closed vocabulary must still be consulted"
