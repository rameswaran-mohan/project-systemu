"""Phase 6 Batch 2 (6g) — +New 'Submit task' surfaces a live Work link.

The chat compose submit ran ``run_direct_task`` in a daemon thread and
DISCARDED the return — the operator had no way to jump from "task done" to
the workflow they just created.  ``run_direct_task`` returns the Activity
(``.scroll_id``; workflow_id == scroll_id), so 6g captures it and, on
completion, surfaces a "View in Work" link to ``/workflow/<scroll_id>``.

The link target is computed by the pure helper ``_work_link_for(activity)``
so it is unit-testable headless:
  - Activity with a scroll_id  -> /workflow/<scroll_id>
  - missing scroll_id / None    -> /work (graceful fallback)
"""
import types

from systemu.interface.pages.chat_page import _work_link_for


def _activity(scroll_id):
    a = types.SimpleNamespace()
    a.scroll_id = scroll_id
    return a


def test_link_points_at_the_workflow_when_scroll_id_present():
    assert _work_link_for(_activity("scroll_123")) == "/workflow/scroll_123"


def test_falls_back_to_work_when_scroll_id_absent():
    # Activity object without a scroll_id attribute at all.
    assert _work_link_for(types.SimpleNamespace()) == "/work"


def test_falls_back_to_work_when_scroll_id_empty():
    assert _work_link_for(_activity("")) == "/work"
    assert _work_link_for(_activity(None)) == "/work"


def test_falls_back_to_work_when_activity_is_none():
    # run_direct_task returns None on early pipeline failure.
    assert _work_link_for(None) == "/work"
