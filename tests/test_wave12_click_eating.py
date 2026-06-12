"""W12 — buttons in timer-refreshed lists must not eat clicks.

Live audit catch (2026-06-12): "Review & Approve" on /work did nothing —
the unconditional 2s repaint destroyed and rebuilt every row button, so a
click arriving for the just-destroyed element was silently dropped. Same
class on the rail (Answer/Approve, 2s) and /inbox (5s). And Home's
"Record" tile called a navigate-to-/ fallback that visibly did nothing on
Home itself.

Fix: ``gated_refresh`` — the tick repaints ONLY when the fingerprinted
model changed (idle pages stop rebuilding entirely), plus the real record-
dialog opener stashed per-client by the layout.
"""
from __future__ import annotations

import inspect


class TestGatedRefresh:
    def test_first_tick_paints_then_idles(self):
        from systemu.interface.ui_helpers import gated_refresh
        paints = []
        tick = gated_refresh(lambda: "same", lambda: paints.append(1))
        tick(); tick(); tick()
        assert len(paints) == 1, "an idle page must stop rebuilding"

    def test_change_triggers_exactly_one_paint(self):
        from systemu.interface.ui_helpers import gated_refresh
        paints = []
        values = iter(["a", "a", "b", "b"])
        tick = gated_refresh(lambda: next(values), lambda: paints.append(1))
        tick(); tick(); tick(); tick()
        assert len(paints) == 2

    def test_fingerprint_error_fails_open(self):
        from systemu.interface.ui_helpers import gated_refresh
        paints = []

        def _boom():
            raise RuntimeError("fingerprint broke")

        tick = gated_refresh(_boom, lambda: paints.append(1))
        tick(); tick()
        assert len(paints) == 1, "liveness beats stability: paint once, then idle"


class TestSurfacesAreGated:
    def test_work_rows_are_change_gated(self):
        from systemu.interface.pages import work
        src = inspect.getsource(work)
        assert "gated_refresh(" in src
        assert "safe_timer(2.0, _rows_view.refresh)" not in src, \
            "unconditional repaints eat Review & Approve clicks"

    def test_inbox_rail_is_change_gated(self):
        from systemu.interface.components import inbox_rail
        src = inspect.getsource(inbox_rail)
        assert "gated_refresh(" in src
        assert "safe_timer(2.0, _pane.refresh)" not in src

    def test_inbox_page_is_change_gated(self):
        from systemu.interface.pages import inbox_page
        src = inspect.getsource(inbox_page)
        assert "gated_refresh(" in src
        assert "safe_timer(5.0, _refresh_all)" not in src


class TestDecisionQueueDefault:
    def test_daemon_defaults_decision_queue_on(self):
        """W12 (ship-blocker): without SYSTEMU_DECISION_QUEUE=true, operator
        asks (e.g. 'New Shadow Recommended' after the FIRST recorded
        workflow) silently auto-skip headless and the pipeline dead-ends at
        an unassigned activity — on every default install. Under the daemon
        there IS a queue + dashboard; route to it by default (setdefault, so
        an explicit operator false still wins)."""
        from systemu.scheduler import daemon
        src = inspect.getsource(daemon)
        assert 'setdefault("SYSTEMU_DECISION_QUEUE", "true")' in src


class TestHomeRecordTile:
    def test_layout_stashes_the_real_opener(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard)
        assert "systemu_open_record_dialog = _open_record_dialog" in src

    def test_home_uses_the_stashed_opener(self):
        from systemu.interface.pages import console
        src = inspect.getsource(console._trigger_record_dialog)
        assert "systemu_open_record_dialog" in src, \
            "Home's Record tile must open the REAL dialog, not navigate-to-/"
