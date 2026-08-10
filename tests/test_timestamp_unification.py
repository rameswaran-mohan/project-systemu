"""One stored timestamp must render as the SAME local string on every surface.

Field defect (v0.10.23, operator zone Asia/Kolkata): a single "Supervisor
started" event rendered ``20:16:56`` in the Home page right rail and
``01:46:56`` in the Chat page's Live Events feed - a 5h30m split - while the
Work page's workflow card printed ``2026-08-10 20:56:54`` for a run whose feed
stamps read ``02:24``.

Storage convention (the thing that decides who is wrong):
  * ``systemu.interface.event_bus`` stamps every event's ``ts`` with
    ``datetime.now(timezone.utc).isoformat()``  -> tz-AWARE UTC.
  * ``systemu.runtime.workflow_tracker._now`` does the same for
    ``started_at`` / ``updated_at`` / timeline entries.
  * ``systemu.core.utils.utcnow`` returns a NAIVE datetime that holds UTC.
So EVERY dashboard timestamp is UTC. The rail, the events pane and the Work
card printed those UTC digits verbatim; only the Chat feed converted. The
product's audience is a desk operator, not a server admin, so LOCAL wall-clock
is the correct rendering and the Chat feed was the only surface that was right.

These tests pin the operator's real zone explicitly (``tz=`` is a test seam)
so they assert the true conversion rather than re-deriving it from whatever
zone the test host happens to sit in.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")

# The exact instant from the field report.
STORED_AWARE = "2026-08-10T20:16:56+00:00"
STORED_Z = "2026-08-10T20:16:56Z"
STORED_NAIVE = "2026-08-10T20:16:56"          # utcnow()-shaped: naive, holds UTC
LOCAL_TIME = "01:46:56"                        # the operator's wall clock
LOCAL_STAMP = "2026-08-11 01:46:56"
UTC_TIME = "20:16:56"                          # what the broken surfaces showed


# ---------------------------------------------------------------------------
# The storage convention itself - this is what makes LOCAL the right answer.
# ---------------------------------------------------------------------------

class TestStorageConventionIsUtc:
    def test_event_bus_stamps_aware_utc(self):
        from systemu.interface.event_bus import EventBus

        event = {"level": "INFO", "message": "Supervisor started"}
        EventBus().publish(event)
        dt = datetime.fromisoformat(event["ts"])
        assert dt.tzinfo is not None, "EventBus ts must be tz-aware"
        assert dt.utcoffset().total_seconds() == 0, "EventBus ts must be UTC"

    def test_workflow_tracker_stamps_aware_utc(self):
        from systemu.runtime.workflow_tracker import _now

        dt = datetime.fromisoformat(_now())
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0

    def test_utcnow_is_naive_but_holds_utc(self):
        """The naive producer - this is WHY the helper assumes UTC for naive."""
        from systemu.core.utils import utcnow

        naive = utcnow()
        assert naive.tzinfo is None
        drift = abs((naive - datetime.now(timezone.utc).replace(tzinfo=None))
                    .total_seconds())
        assert drift < 5, "utcnow() must hold UTC, not local time"


# ---------------------------------------------------------------------------
# The shared helper.
# ---------------------------------------------------------------------------

class TestSharedFormatter:
    def test_aware_utc_renders_operator_local(self):
        from systemu.interface.ui_helpers import format_event_time

        assert format_event_time(STORED_AWARE, tz=IST) == LOCAL_TIME
        assert format_event_time(STORED_Z, tz=IST) == LOCAL_TIME

    def test_naive_timestamp_is_assumed_utc(self):
        """Documented + asserted: a naive stored timestamp is UTC, not local.

        Every naive producer in this codebase is ``core.utils.utcnow`` which
        returns UTC. Reading a naive stamp as local time would leave the value
        unconverted - the exact defect this module exists to prevent.
        """
        from systemu.interface.ui_helpers import format_event_time, to_local_datetime

        assert format_event_time(STORED_NAIVE, tz=IST) == LOCAL_TIME
        assert to_local_datetime(STORED_NAIVE, tz=IST) == \
            to_local_datetime(STORED_AWARE, tz=IST)

    def test_epoch_seconds_render_local(self):
        from systemu.interface.ui_helpers import format_event_time

        epoch = datetime(2026, 8, 10, 20, 16, 56, tzinfo=timezone.utc).timestamp()
        assert format_event_time(epoch, tz=IST) == LOCAL_TIME

    def test_datetime_input_renders_local(self):
        from systemu.interface.ui_helpers import format_event_time

        aware = datetime(2026, 8, 10, 20, 16, 56, tzinfo=timezone.utc)
        assert format_event_time(aware, tz=IST) == LOCAL_TIME
        # naive datetime -> same assumed-UTC rule
        assert format_event_time(aware.replace(tzinfo=None), tz=IST) == LOCAL_TIME

    def test_full_stamp_carries_the_local_date_too(self):
        """The date rolls with the clock - 08-10 UTC is 08-11 for the operator."""
        from systemu.interface.ui_helpers import format_stamp

        assert format_stamp(STORED_AWARE, tz=IST) == LOCAL_STAMP

    def test_missing_or_garbage_renders_empty(self):
        from systemu.interface.ui_helpers import format_event_time, format_stamp

        for bad in (None, "", "not-a-time"):
            assert format_event_time(bad, tz=IST) == ""
            assert format_stamp(bad, tz=IST) == ""

    def test_default_tz_is_the_system_local_zone(self):
        from systemu.interface.ui_helpers import to_local_datetime

        got = to_local_datetime(STORED_AWARE)
        expected = datetime(2026, 8, 10, 20, 16, 56, tzinfo=timezone.utc).astimezone()
        assert got == expected
        assert got.utcoffset() == expected.utcoffset()


# ---------------------------------------------------------------------------
# THE regression: every surface, one stored value, one string.
# ---------------------------------------------------------------------------

def _surface_times(stored, tz):
    """HH:MM:SS as each production surface renders `stored`."""
    from systemu.interface.components.live_events_pane import _format_event_time
    from systemu.interface.components.right_rail import live_event_row
    from systemu.interface.pages import work, workflow_detail

    return {
        # /chat + /insights Manual Logs + the pane's own rows
        "live_events_pane": _format_event_time(stored, tz=tz),
        # Home page right rail (its row model is the production call site)
        "home_right_rail": live_event_row(
            {"ts": stored, "message": "Supervisor started"}, tz=tz)["time"],
        # Work page workflow card (date + time; compare the clock portion)
        "work_card": work._short_ts(stored, tz=tz)[11:],
        # Workflow detail stats + timeline
        "workflow_detail": workflow_detail._short_ts(stored, tz=tz)[11:],
    }


class TestEverySurfaceAgrees:
    @pytest.mark.parametrize("stored", [STORED_AWARE, STORED_Z, STORED_NAIVE])
    def test_one_stored_timestamp_one_rendered_string(self, stored):
        rendered = _surface_times(stored, IST)
        assert len(set(rendered.values())) == 1, (
            "surfaces disagree on the same stored timestamp: " + repr(rendered))

    @pytest.mark.parametrize("stored", [STORED_AWARE, STORED_Z, STORED_NAIVE])
    def test_that_string_is_the_operators_local_time(self, stored):
        rendered = _surface_times(stored, IST)
        for surface, value in rendered.items():
            assert value == LOCAL_TIME, f"{surface} rendered {value!r}"
            assert value != UTC_TIME, f"{surface} still prints raw UTC"

    def test_work_card_shows_the_local_date(self):
        """The field-reported card read 2026-08-10 20:56:54 - raw UTC."""
        from systemu.interface.pages import work

        assert work._short_ts(STORED_AWARE, tz=IST) == LOCAL_STAMP

    def test_surfaces_agree_in_the_hosts_own_zone(self):
        """No tz seam: the default path must agree across surfaces too."""
        rendered = _surface_times(STORED_AWARE, None)
        assert len(set(rendered.values())) == 1, repr(rendered)


# ---------------------------------------------------------------------------
# Reachability pins - delete the production wiring and a NAMED test goes red.
# ---------------------------------------------------------------------------

class TestSurfacesUseTheSharedHelper:
    """Reachability pins.

    The pane / rail / work / detail surfaces are pinned by CALLING their real
    production functions in ``TestEverySurfaceAgrees``. The Chat feed and the
    scroll journal format inside nested render closures that need a NiceGUI
    slot, so an import-identity assertion alone would pass even if the call
    site were re-inlined -- these pin the call site in source too.
    """

    def test_chat_live_feed_imports_the_shared_formatter(self):
        from systemu.interface import ui_helpers
        from systemu.interface.pages import systemu_chat

        assert systemu_chat.format_event_time is ui_helpers.format_event_time

    def test_chat_live_feed_calls_it_and_keeps_no_private_conversion(self):
        import inspect

        from systemu.interface.pages import systemu_chat

        src = inspect.getsource(systemu_chat)
        assert "format_event_time(ts_raw)" in src, \
            "the chat feed no longer routes its stamp through the shared helper"
        assert ".astimezone(" not in src, "chat re-inlined a private conversion"
        assert "[11:19]" not in src, "chat re-inlined a private ISO slice"

    def test_scroll_journal_calls_it_and_keeps_no_private_conversion(self):
        import inspect

        from systemu.interface.pages import scrolls

        src = inspect.getsource(scrolls)
        assert "format_event_time(" in src, \
            "the scroll journal no longer routes its stamp through the helper"
        assert ".strftime(" not in src, "scrolls re-inlined a private conversion"

    def test_live_events_pane_imports_the_shared_formatter(self):
        from systemu.interface import ui_helpers
        from systemu.interface.components import live_events_pane

        assert live_events_pane.format_event_time is ui_helpers.format_event_time

    def test_work_and_detail_import_the_shared_stamp_formatter(self):
        from systemu.interface import ui_helpers
        from systemu.interface.pages import work, workflow_detail

        assert work.format_stamp is ui_helpers.format_stamp
        assert workflow_detail.format_stamp is ui_helpers.format_stamp

    def test_scroll_journal_imports_the_shared_formatter(self):
        from systemu.interface import ui_helpers
        from systemu.interface.pages import scrolls

        assert scrolls.format_event_time is ui_helpers.format_event_time


class TestScrollJournalTimeline:
    """The scroll page's journal renders ``TraceEvent.ts`` (a datetime).

    ``systemu.core.models`` defaults it to ``datetime.now(tz=timezone.utc)``,
    so it is aware UTC like every other dashboard timestamp; the page used to
    call ``.strftime`` on it directly and printed the UTC clock.
    """

    def test_trace_event_ts_default_is_aware_utc(self):
        from systemu.core.models import TraceEvent

        ts = TraceEvent(stage="intent", level="info", message="captured").ts
        assert ts.tzinfo is not None
        assert ts.utcoffset().total_seconds() == 0

    def test_journal_time_matches_the_other_surfaces(self):
        from systemu.interface.ui_helpers import format_event_time

        aware = datetime(2026, 8, 10, 20, 16, 56, tzinfo=timezone.utc)
        assert format_event_time(aware, tz=IST) == LOCAL_TIME
        assert format_event_time(aware, tz=IST) == \
            _surface_times(STORED_AWARE, IST)["live_events_pane"]
