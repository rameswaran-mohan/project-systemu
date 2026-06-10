"""Phase 5 Slice 2a — pure models for the /work workflow-centric list page.

Covers (no NiceGUI runtime needed):
  * ``work_row_model``   — WorkflowSnapshot → row dict (reached chips, links,
    status tinting, needs_approval);
  * ``_unlinked_activities`` — THIN defensive fallback for activities whose
    scroll vanished (coverage is 100% by construction; this guards drift);
  * ``_filter_rows`` — the search + status-filter helper the page composes.
"""
from systemu.interface.pages.work import (
    _filter_rows,
    _unlinked_activities,
    work_row_model,
)
from systemu.runtime.workflow_tracker import STAGES, WorkflowSnapshot


def _snap(**kw) -> WorkflowSnapshot:
    base = dict(
        workflow_id="wf_1",
        title="Deploy web page",
        stage="activity",
        status="assigned",
    )
    base.update(kw)
    return WorkflowSnapshot(**base)


# ── work_row_model: shape + reached chips ────────────────────────────────────

class TestWorkRowModel:
    def test_row_shape(self):
        row = work_row_model(_snap())
        assert row["workflow_id"] == "wf_1"
        assert row["title"] == "Deploy web page"
        assert row["status"] == "assigned"
        assert row["detail_link"] == "/workflow/wf_1"
        assert isinstance(row["updated_at"], str)
        assert [c["stage"] for c in row["chips"]] == STAGES

    def test_mid_pipeline_reached_flags(self):
        # stage=activity → capture/scroll/activity reached; execution/done not.
        row = work_row_model(_snap(stage="activity"))
        reached = {c["stage"]: c["reached"] for c in row["chips"]}
        assert reached == {
            "capture": True, "scroll": True, "activity": True,
            "execution": False, "done": False,
        }

    def test_timeline_entry_marks_stage_reached(self):
        # Precedent: pages/workflow_detail.py — a timeline timestamp counts
        # as reached even when the snapshot's stage pointer sits earlier.
        snap = _snap(stage="scroll")
        snap.timeline["execution"] = "2026-06-10T00:00:00+00:00"
        row = work_row_model(snap)
        reached = {c["stage"]: c["reached"] for c in row["chips"]}
        assert reached["execution"] is True
        assert reached["done"] is False

    def test_capture_chip_always_reached_and_passive(self):
        row = work_row_model(_snap(stage="capture"))
        capture = row["chips"][0]
        assert capture["stage"] == "capture"
        assert capture["reached"] is True
        assert capture["link"] is None

    def test_failed_stage_does_not_crash(self):
        # "failed" is terminal but NOT in STAGES — rank must degrade
        # gracefully (only timeline entries count as reached).
        snap = _snap(stage="failed", status="failed")
        snap.timeline["execution"] = "2026-06-10T00:00:00+00:00"
        row = work_row_model(snap)
        reached = {c["stage"]: c["reached"] for c in row["chips"]}
        assert reached["capture"] is True          # capture is always reached
        assert reached["execution"] is True        # via timeline
        assert reached["done"] is False


# ── work_row_model: chip links ───────────────────────────────────────────────

class TestChipLinks:
    def test_reached_chips_link_to_their_surfaces(self):
        snap = _snap(
            stage="done", status="completed",
            scroll_id="wf_1", activity_id="act_1", shadow_id="sh_1",
            execution_id="exec_1",
        )
        links = {c["stage"]: c["link"] for c in work_row_model(snap)["chips"]}
        assert links["capture"] is None            # passive
        assert links["scroll"] == "/scrolls"
        assert links["activity"] == "/activities"
        assert links["execution"] == "/army"       # shadow runs it
        assert links["done"] == "/workflow/wf_1"

    def test_unreached_chips_have_no_link(self):
        snap = _snap(stage="scroll", scroll_id="wf_1")
        links = {c["stage"]: c["link"] for c in work_row_model(snap)["chips"]}
        assert links["activity"] is None
        assert links["execution"] is None
        assert links["done"] is None

    def test_execution_chip_falls_back_to_workflow_detail(self):
        # Reached execution with no shadow yet → the workflow detail page.
        snap = _snap(stage="execution", status="running", scroll_id="wf_1")
        links = {c["stage"]: c["link"] for c in work_row_model(snap)["chips"]}
        assert links["execution"] == "/workflow/wf_1"

    def test_reached_chip_without_entity_has_no_link(self):
        # Reached activity stage but no activity_id recorded → no dead link.
        snap = _snap(stage="activity", activity_id=None)
        links = {c["stage"]: c["link"] for c in work_row_model(snap)["chips"]}
        assert links["activity"] is None


# ── work_row_model: status tinting + approval affordance ────────────────────

class TestStatusTinting:
    def test_validator_blocked_is_warn(self):
        assert work_row_model(_snap(status="validator_blocked"))["status_class"] == "warn"

    def test_extraction_failed_is_danger(self):
        assert work_row_model(_snap(status="extraction_failed"))["status_class"] == "danger"

    def test_failed_is_danger(self):
        assert work_row_model(_snap(stage="failed", status="failed"))["status_class"] == "danger"

    def test_ordinary_statuses_are_ok(self):
        for status in ("assigned", "running", "completed", "approved"):
            assert work_row_model(_snap(status=status))["status_class"] == "ok", status

    def test_pending_approval_needs_approval_and_warn(self):
        row = work_row_model(_snap(status="pending_approval"))
        assert row["needs_approval"] is True
        assert row["status_class"] == "warn"

    def test_other_statuses_do_not_need_approval(self):
        assert work_row_model(_snap(status="assigned"))["needs_approval"] is False


# ── _unlinked_activities: THIN defensive fallback ────────────────────────────

class TestUnlinkedActivities:
    SCROLLS = [{"id": "scr_1"}, {"id": "scr_2"}]

    def test_all_linked_yields_empty(self):
        acts = [{"id": "a1", "scroll_id": "scr_1"}, {"id": "a2", "scroll_id": "scr_2"}]
        assert _unlinked_activities(self.SCROLLS, acts) == []

    def test_orphan_activity_detected(self):
        acts = [
            {"id": "a1", "scroll_id": "scr_1"},
            {"id": "a2", "scroll_id": "scr_GONE"},
        ]
        out = _unlinked_activities(self.SCROLLS, acts)
        assert [a["id"] for a in out] == ["a2"]

    def test_missing_scroll_id_counts_as_unlinked(self):
        acts = [{"id": "a1"}]                      # no scroll_id key at all
        out = _unlinked_activities(self.SCROLLS, acts)
        assert [a["id"] for a in out] == ["a1"]

    def test_none_inputs_are_safe(self):
        assert _unlinked_activities(None, None) == []
        assert _unlinked_activities([], [{"id": "a1", "scroll_id": "s"}]) != []


# ── _filter_rows: the page's search + status filter ──────────────────────────

class TestFilterRows:
    ROWS = [
        work_row_model(_snap(workflow_id="wf_1", title="Deploy web page",
                             status="assigned")),
        work_row_model(_snap(workflow_id="wf_2", title="Burrito order",
                             status="pending_approval")),
        work_row_model(_snap(workflow_id="wf_3", title="Weekly report",
                             status="validator_blocked")),
    ]

    def test_empty_query_and_all_status_pass_through(self):
        assert _filter_rows(self.ROWS, "", "all") == self.ROWS
        assert _filter_rows(self.ROWS, "", "") == self.ROWS

    def test_query_matches_title_case_insensitive(self):
        out = _filter_rows(self.ROWS, "BURRITO", "all")
        assert [r["workflow_id"] for r in out] == ["wf_2"]

    def test_query_matches_workflow_id(self):
        out = _filter_rows(self.ROWS, "wf_3", "all")
        assert [r["workflow_id"] for r in out] == ["wf_3"]

    def test_status_filter_exact(self):
        out = _filter_rows(self.ROWS, "", "pending_approval")
        assert [r["workflow_id"] for r in out] == ["wf_2"]

    def test_query_and_status_combine(self):
        assert _filter_rows(self.ROWS, "weekly", "pending_approval") == []
        out = _filter_rows(self.ROWS, "weekly", "validator_blocked")
        assert [r["workflow_id"] for r in out] == ["wf_3"]

    def test_no_match_yields_empty(self):
        assert _filter_rows(self.ROWS, "nope-nothing", "all") == []

    def test_none_query_and_status_are_safe(self):
        assert _filter_rows(self.ROWS, None, None) == self.ROWS
