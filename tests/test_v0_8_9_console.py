"""v0.8.9 console refinement tests."""
from __future__ import annotations
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")   # the operator's zone in the field report


class TestEventTimeFormat:
    """Event ``ts`` is stored in UTC; the pane renders the operator's LOCAL
    time (v0.10.24 unification: these cases used to pin the raw UTC digits,
    which is the split the field report caught). ``tz`` is the test seam."""

    def test_iso_string(self):
        from systemu.interface.components.live_events_pane import _format_event_time
        # ISO with Z suffix: both spellings mean the same UTC instant, and
        # both render as that instant's local wall clock (+05:30 here).
        assert _format_event_time("2026-05-30T14:23:45+00:00", tz=IST) == "19:53:45"
        assert _format_event_time("2026-05-30T14:23:45Z", tz=IST) == "19:53:45"

    def test_epoch_float(self):
        from systemu.interface.components.live_events_pane import _format_event_time
        from datetime import datetime, timezone
        epoch = datetime(2026, 5, 30, 14, 23, 45, tzinfo=timezone.utc).timestamp()
        assert _format_event_time(epoch, tz=IST) == "19:53:45"

    def test_missing_or_garbage(self):
        from systemu.interface.components.live_events_pane import _format_event_time
        assert _format_event_time(None) == ""
        assert _format_event_time("") == ""
        assert _format_event_time("not-a-time") == ""


class TestDisplayOrder:
    def test_newest_first(self):
        from systemu.interface.components.live_events_pane import _display_order
        buf = [{"message": "old"}, {"message": "mid"}, {"message": "new"}]
        ordered = _display_order(buf)
        assert [e["message"] for e in ordered] == ["new", "mid", "old"]

    def test_does_not_mutate_input(self):
        from systemu.interface.components.live_events_pane import _display_order
        buf = [{"message": "a"}, {"message": "b"}]
        _display_order(buf)
        # original order preserved (display-order returns a new list)
        assert [e["message"] for e in buf] == ["a", "b"]
