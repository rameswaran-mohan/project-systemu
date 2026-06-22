"""Phase 6 Batch 2 (6e) — notifications scroll-approve routes through the
unified inspect-before-approve gate (the blind approve is retired).

Before 6e the Notifications "Approve" button on a ``scroll_approval``
notification called ``_approve_scroll_from_ui`` which BLIND-approved the
scroll (``approve_pending_scroll`` in a thread, no inspection) — a second
surface that bypassed the unified Inbox card.

6e replaces that call with ``open_scroll_review_dialog`` (Phase 5's
inspect-before-approve gate).  We assert the scroll_approval+approve path:
  - calls ``open_scroll_review_dialog`` with the scroll_id + an on_resolved
    callable (so the page refreshes after the operator decides);
  - does NOT blind-approve via ``approve_pending_scroll``;
  - the dead ``_approve_scroll_from_ui`` helper is gone.
"""
import types
from unittest import mock

import systemu.interface.pages.notifications_page as np


def test_scroll_approve_opens_the_review_dialog_not_a_blind_approve():
    ctx = {"notification_type": "scroll_approval", "scroll_id": "scroll_abc"}
    refresh_fn = mock.Mock()

    with mock.patch.object(np, "open_scroll_review_dialog") as open_dlg, \
         mock.patch.object(np, "ui") as ui_mock:
        np._dispatch_notification_action("Approve", ctx, vault=mock.Mock(), refresh_fn=refresh_fn)

    # Routed through the unified inspect-card gate…
    assert open_dlg.call_count == 1
    args, kwargs = open_dlg.call_args
    # scroll_id passed (positionally or by keyword)
    assert (args and args[0] == "scroll_abc") or kwargs.get("scroll_id") == "scroll_abc"
    # on_resolved is wired so the page refreshes after the decision
    assert callable(kwargs.get("on_resolved"))


def test_missing_scroll_id_does_not_open_the_dialog():
    ctx = {"notification_type": "scroll_approval"}  # no scroll_id
    with mock.patch.object(np, "open_scroll_review_dialog") as open_dlg, \
         mock.patch.object(np, "ui"):
        np._dispatch_notification_action("Approve", ctx, vault=mock.Mock(), refresh_fn=mock.Mock())
    open_dlg.assert_not_called()


def test_blind_approve_helper_is_retired():
    assert not hasattr(np, "_approve_scroll_from_ui"), \
        "_approve_scroll_from_ui (the blind scroll-approve) must be deleted"


def test_dispatch_module_does_not_call_approve_pending_scroll_directly():
    # No blind approval path: invoking the scroll_approval+approve branch must
    # not reach approve_pending_scroll. We sentinel it and assert it never runs.
    import systemu.pipelines.scroll_refiner as sr
    sentinel = mock.Mock(side_effect=AssertionError("blind approve_pending_scroll called"))
    ctx = {"notification_type": "scroll_approval", "scroll_id": "scroll_abc"}
    with mock.patch.object(np, "open_scroll_review_dialog"), \
         mock.patch.object(np, "ui"), \
         mock.patch.object(sr, "approve_pending_scroll", sentinel):
        np._dispatch_notification_action("Approve", ctx, vault=mock.Mock(), refresh_fn=mock.Mock())
    sentinel.assert_not_called()
