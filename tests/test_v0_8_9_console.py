"""v0.8.9 console refinement tests."""
from __future__ import annotations
import pytest


class TestEventTimeFormat:
    def test_iso_string(self):
        from systemu.interface.components.live_events_pane import _format_event_time
        # ISO with Z suffix
        assert _format_event_time("2026-05-30T14:23:45+00:00") == "14:23:45"
        assert _format_event_time("2026-05-30T14:23:45Z") == "14:23:45"

    def test_epoch_float(self):
        from systemu.interface.components.live_events_pane import _format_event_time
        from datetime import datetime, timezone
        epoch = datetime(2026, 5, 30, 14, 23, 45, tzinfo=timezone.utc).timestamp()
        assert _format_event_time(epoch) == "14:23:45"

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
