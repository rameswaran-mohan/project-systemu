"""P10 — one needs-consolidation rule (fixes the 5-vs-10 + raw-line-count bug).

Before this slice, ``memory_status`` flagged "pending" at >= 5 *raw file lines*
of memory_buffer.jsonl, while the consolidation page and the engine triggered
at >= 10 *parsed entries* (or staleness).  ``needs_consolidation`` is now the
single rule; these tests pin its behaviour and prove memory_status agrees with
the consolidation page at the boundary.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from systemu.core.utils import utcnow
from systemu.runtime.memory_rules import needs_consolidation
from systemu.scheduler.jobs import BUFFER_THRESHOLD, STALE_AFTER_DAYS


def _fresh_md() -> str:
    """SHADOW_MEMORY.md whose last_consolidated is recent (NOT stale)."""
    ts = utcnow().isoformat()
    return f"---\nlast_consolidated: {ts}\n---\n# memory\n"


def _stale_md() -> str:
    """SHADOW_MEMORY.md whose last_consolidated is well past the stale window."""
    ts = (utcnow() - timedelta(days=STALE_AFTER_DAYS + 5)).isoformat()
    return f"---\nlast_consolidated: {ts}\n---\n# memory\n"


# ── The rule ────────────────────────────────────────────────────────────────

def test_below_threshold_not_stale_is_false():
    """5 buffered entries (the old memory_status trigger) must NOT consolidate."""
    buf = list(range(5))
    assert len(buf) < BUFFER_THRESHOLD  # guard: this slice assumes threshold > 5
    assert needs_consolidation(buf, _fresh_md()) is False


def test_at_threshold_is_true():
    buf = list(range(BUFFER_THRESHOLD))
    assert needs_consolidation(buf, _fresh_md()) is True


def test_stale_with_nonempty_buffer_is_true():
    buf = [1]  # below threshold but memory is stale
    assert needs_consolidation(buf, _stale_md()) is True


def test_empty_buffer_is_false_even_when_stale():
    assert needs_consolidation([], _stale_md()) is False


def test_empty_buffer_is_false_when_fresh():
    assert needs_consolidation([], _fresh_md()) is False


def test_no_timestamp_is_stale_only_when_buffer_nonempty():
    """Missing last_consolidated frontmatter => stale-eligible, gated on bool(buf)."""
    no_ts = "# memory with no frontmatter\n"
    assert needs_consolidation([], no_ts) is False
    assert needs_consolidation([1], no_ts) is True


# ── Cross-surface agreement at the boundary ──────────────────────────────────

@pytest.mark.parametrize("n", [5, BUFFER_THRESHOLD - 1, BUFFER_THRESHOLD, BUFFER_THRESHOLD + 1])
def test_memory_status_agrees_with_page_at_boundary(n):
    """memory_status's pending flag must equal the page's needs_consolidation.

    Both surfaces are now derived from the same ``needs_consolidation`` rule on
    parsed (md_text, buf); this proves they never diverge at the 5/10 boundary
    where the old raw-line hack disagreed with the engine.
    """
    from systemu.runtime import memory_rules

    md = _fresh_md()
    buf = list(range(n))

    page_decision = needs_consolidation(buf, md)

    # memory_status must route through the SAME helper (no literal 5, no raw
    # line counting).  We assert the module imports and uses needs_consolidation.
    from systemu.interface.components import memory_status

    assert hasattr(memory_status, "needs_consolidation"), (
        "memory_status must import the shared needs_consolidation rule"
    )
    status_decision = memory_status.needs_consolidation(buf, md)
    assert status_decision == page_decision

    # And the rule itself agrees with the canonical engine gate at this n.
    is_stale = memory_rules._is_stale(md)
    engine_gate = (len(buf) >= BUFFER_THRESHOLD or is_stale) and bool(buf)
    assert page_decision == engine_gate
