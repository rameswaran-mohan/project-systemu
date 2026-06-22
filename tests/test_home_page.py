"""Home spine (Phase 6) — at-a-glance dashboard tests.

The spec (§5 + §4.2): Home is "at-a-glance: what's running, what needs me.
Cards are LINKS, not re-renders of other pages." These tests pin the PURE
summary models that drive the two glance cards — the NiceGUI rendering is a
thin (untested) shell, like the other page builders.

  * ``home_needs_you_summary(descriptors)`` → {count, top:[titles], link}
    — a SUMMARY of the pending gates that links to /inbox (NOT the full
    resolvable cards — those live in the Inbox + right rail).
  * ``home_recent_workflows(snapshots, limit)`` → recent rows that each
    LINK to /workflow/{id} (NOT a re-render of the Work list).
"""
from __future__ import annotations

from systemu.interface.pages.console import (
    home_needs_you_summary,
    home_recent_workflows,
)


# ─────────────────────────────────────────────────────────────────────────────
#  "What needs you" summary — count + top titles + /inbox link
# ─────────────────────────────────────────────────────────────────────────────


class _Desc:
    """Minimal GateDescriptor stand-in (only .title is read)."""

    def __init__(self, title: str):
        self.title = title


def _descriptors(*titles):
    """Build the (id, descriptor) tuples list_descriptors() returns."""
    return [(f"dec_{i}", _Desc(t)) for i, t in enumerate(titles)]


class TestNeedsYouSummary:
    def test_empty_is_zero_with_link_and_no_titles(self):
        s = home_needs_you_summary([])
        assert s["count"] == 0
        assert s["top"] == []
        assert s["link"] == "/inbox"

    def test_count_matches_descriptor_count(self):
        s = home_needs_you_summary(_descriptors("a", "b", "c", "d", "e"))
        assert s["count"] == 5
        assert s["link"] == "/inbox"

    def test_top_caps_at_three_titles_in_order(self):
        s = home_needs_you_summary(_descriptors("first", "second", "third", "fourth"))
        # count reflects ALL pending; the glance shows only the leading 3
        assert s["count"] == 4
        assert s["top"] == ["first", "second", "third"]

    def test_top_returns_all_when_fewer_than_three(self):
        s = home_needs_you_summary(_descriptors("only"))
        assert s["count"] == 1
        assert s["top"] == ["only"]

    def test_missing_title_falls_back_to_placeholder(self):
        # A descriptor with an empty title must not vanish or crash — the count
        # still reflects it and a placeholder keeps the glance readable.
        s = home_needs_you_summary([("dec_0", _Desc(""))])
        assert s["count"] == 1
        assert s["top"] == ["(untitled gate)"]

    def test_tolerates_non_descriptor_rows(self):
        # Defensive: a malformed row (no .title attr) must not break the glance.
        class _NoTitle:
            pass

        s = home_needs_you_summary([("dec_0", _NoTitle())])
        assert s["count"] == 1
        assert s["top"] == ["(untitled gate)"]


# ─────────────────────────────────────────────────────────────────────────────
#  "Recent activity" — recent workflows, newest first, each linking out
# ─────────────────────────────────────────────────────────────────────────────


class _Snap:
    """Minimal WorkflowSnapshot stand-in for the recent-activity selector."""

    def __init__(self, wid, title, status, stage, updated_at):
        self.workflow_id = wid
        self.title = title
        self.status = status
        self.stage = stage
        self.updated_at = updated_at


class TestRecentWorkflows:
    def test_empty_is_empty(self):
        assert home_recent_workflows([]) == []

    def test_sorted_newest_first(self):
        snaps = [
            _Snap("wf_a", "A", "running", "execution", "2026-06-01T00:00:00"),
            _Snap("wf_b", "B", "done", "done", "2026-06-03T00:00:00"),
            _Snap("wf_c", "C", "running", "scroll", "2026-06-02T00:00:00"),
        ]
        rows = home_recent_workflows(snaps)
        assert [r["title"] for r in rows] == ["B", "C", "A"]

    def test_caps_at_limit(self):
        snaps = [
            _Snap(f"wf_{i}", f"T{i}", "running", "scroll", f"2026-06-{i+1:02d}T00:00:00")
            for i in range(10)
        ]
        rows = home_recent_workflows(snaps, limit=4)
        assert len(rows) == 4
        # newest first → the highest day numbers
        assert rows[0]["title"] == "T9"

    def test_row_links_to_workflow_detail(self):
        snaps = [_Snap("wf_x", "X", "running", "execution", "2026-06-01T00:00:00")]
        row = home_recent_workflows(snaps)[0]
        assert row["link"] == "/workflow/wf_x"
        assert row["title"] == "X"
        assert row["status"] == "running"
        assert row["stage"] == "execution"

    def test_missing_updated_at_sorts_last_without_crash(self):
        snaps = [
            _Snap("wf_a", "A", "running", "scroll", None),
            _Snap("wf_b", "B", "running", "scroll", "2026-06-02T00:00:00"),
        ]
        rows = home_recent_workflows(snaps)
        assert [r["title"] for r in rows] == ["B", "A"]
