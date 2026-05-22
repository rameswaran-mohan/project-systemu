"""Unit tests for the WorkflowTracker (UX Phase 2)."""

from __future__ import annotations

import pytest

from systemu.runtime.workflow_tracker import (
    STAGES,
    TERMINAL_STAGES,
    WorkflowSnapshot,
    WorkflowTracker,
)


@pytest.fixture
def tracker() -> WorkflowTracker:
    """Fresh tracker per test — reset the singleton state."""
    t = WorkflowTracker.get()
    t.reset()
    yield t
    t.reset()


# ── upsert + read-side -------------------------------------------------

def test_upsert_creates_snapshot(tracker):
    snap = tracker.upsert("wf-1", stage="scroll", status="pending_approval", title="My workflow")
    assert isinstance(snap, WorkflowSnapshot)
    assert snap.workflow_id == "wf-1"
    assert snap.stage == "scroll"
    assert snap.status == "pending_approval"
    assert snap.title == "My workflow"
    assert "scroll" in snap.timeline


def test_upsert_advances_stage(tracker):
    tracker.upsert("wf-2", stage="capture")
    tracker.upsert("wf-2", stage="scroll")
    tracker.upsert("wf-2", stage="activity")
    snap = tracker.get_workflow("wf-2")
    assert snap.stage == "activity"
    assert {"capture", "scroll", "activity"} <= snap.timeline.keys()


def test_upsert_does_not_downgrade_stage(tracker):
    tracker.upsert("wf-3", stage="execution")
    tracker.upsert("wf-3", stage="scroll")  # would be a downgrade
    snap = tracker.get_workflow("wf-3")
    assert snap.stage == "execution"


def test_counts_by_stage_groups_correctly(tracker):
    tracker.upsert("a", stage="scroll")
    tracker.upsert("b", stage="scroll")
    tracker.upsert("c", stage="activity")
    tracker.upsert("d", stage="execution")
    tracker.upsert("e", stage="done")

    counts = tracker.counts_by_stage()
    assert counts["scroll"] == 2
    assert counts["activity"] == 1
    assert counts["execution"] == 1
    assert counts["done"] == 1
    # capture has zero
    assert counts["capture"] == 0


def test_list_active_excludes_terminal(tracker):
    tracker.upsert("alive", stage="execution")
    tracker.upsert("done",  stage="done")

    active_ids = {s.workflow_id for s in tracker.list_active()}
    assert active_ids == {"alive"}

    all_ids = {s.workflow_id for s in tracker.list_all()}
    assert all_ids == {"alive", "done"}


# ── vault reconstruction ----------------------------------------------

class _StubVault:
    """Minimal vault stub for reconstruction tests."""

    def __init__(self, scrolls, activities):
        self._scrolls = scrolls
        self._activities = activities

    def load_index(self, name):
        if name == "scrolls":
            return self._scrolls
        if name == "activities":
            return self._activities
        return []


def test_reconstruct_from_vault_seeds_workflows(tracker):
    vault = _StubVault(
        scrolls=[
            {"id": "scroll-1", "name": "Task A", "status": "pending_approval"},
            {"id": "scroll-2", "name": "Task B", "status": "approved"},
        ],
        activities=[
            {"id": "act-1", "scroll_id": "scroll-2", "shadow_id": "shadow-x",
             "status": "running"},
        ],
    )
    WorkflowTracker.init(vault=vault, events=None)

    snap_a = tracker.get_workflow("scroll-1")
    snap_b = tracker.get_workflow("scroll-2")
    assert snap_a is not None and snap_a.stage == "scroll"
    assert snap_b is not None and snap_b.stage == "execution"
    assert snap_b.activity_id == "act-1"
    assert snap_b.shadow_id == "shadow-x"


# ── event handling ---------------------------------------------------

def test_handle_event_advances_workflow(tracker):
    tracker.upsert("scroll-7", stage="activity")

    tracker._handle_event({
        "category": "shadow",
        "context": {
            "scroll_id": "scroll-7",
            "execution_id": "exec-abc",
            "shadow_id": "shadow-z",
            "status": "running",
        },
    })
    snap = tracker.get_workflow("scroll-7")
    assert snap.stage == "execution"
    assert snap.execution_id == "exec-abc"
    assert snap.shadow_id == "shadow-z"


def test_handle_event_marks_completion(tracker):
    tracker.upsert("scroll-9", stage="execution")
    tracker._handle_event({
        "category": "shadow",
        "context": {"scroll_id": "scroll-9", "status": "completed"},
    })
    snap = tracker.get_workflow("scroll-9")
    # The event handler maps category=shadow → execution stage, but a
    # status of 'completed' is recorded.  The dashboard subsequently
    # interprets that for the "done" stage on the next supervisor event.
    assert snap.status == "completed"


def test_handle_event_without_anchor_is_ignored(tracker):
    # No scroll_id / workflow_id → no upsert, no exception.
    tracker._handle_event({
        "category": "supervisor",
        "context": {"message": "heartbeat"},
    })
    assert tracker.list_all() == []


# ── stage ordering constants -----------------------------------------

def test_stages_order_is_stable():
    assert STAGES == ["capture", "scroll", "activity", "execution", "done"]


def test_terminal_stages_include_done_and_failed():
    assert "done" in TERMINAL_STAGES
    assert "failed" in TERMINAL_STAGES
