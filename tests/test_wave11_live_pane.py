"""W11.1 — the live-pane expand arrow must survive timer repaints.

Field report (2026-06-12): "the drop down arrow inside live pane is not
working correctly." Root cause: the two timer-driven @ui.refreshable panes
(live_events_pane, right_rail.live_runs_pane) rebuilt every row as
``ui.expansion(value=False)`` on every 0.5 s tick — an opened row was
destroyed and rebuilt collapsed within half a second. The arrow "worked";
its state was thrown away.

Fix: stable per-event identity (``event_ui_key``) + caller-owned open state
restored on rebuild (``stateful_expansion``) + repaint-only-when-dirty
(``RepaintGate`` — an idle pane stops re-rendering entirely).
"""
from __future__ import annotations

import inspect


class TestEventUiKey:
    def test_stable_and_idempotent(self):
        from systemu.interface.ui_helpers import event_ui_key
        ev = {"message": "hello"}
        assert event_ui_key(ev) == event_ui_key(ev)

    def test_identity_not_content(self):
        """Two distinct event dicts with equal content are different rows."""
        from systemu.interface.ui_helpers import event_ui_key
        assert event_ui_key({"m": 1}) != event_ui_key({"m": 1})

    def test_shared_dict_shares_key_across_panes(self):
        """EventBus hands the SAME dict to every subscriber — both panes must
        agree on its identity (the stamp is idempotent)."""
        from systemu.interface.ui_helpers import event_ui_key
        ev = {"message": "x"}
        first = event_ui_key(ev)
        # a second pane rendering the same object later
        assert event_ui_key(ev) == first


class TestRepaintGate:
    def test_first_tick_paints(self):
        """Replayed history must show immediately on a fresh pane."""
        from systemu.interface.ui_helpers import RepaintGate
        assert RepaintGate().should_paint() is True

    def test_idle_pane_stops_painting(self):
        from systemu.interface.ui_helpers import RepaintGate
        g = RepaintGate()
        g.should_paint()
        assert g.should_paint() is False
        assert g.should_paint() is False

    def test_bump_triggers_exactly_one_paint(self):
        from systemu.interface.ui_helpers import RepaintGate
        g = RepaintGate()
        g.should_paint()
        g.bump()
        assert g.should_paint() is True
        assert g.should_paint() is False

    def test_many_bumps_coalesce_into_one_paint(self):
        """A burst of events between ticks costs one repaint, not N."""
        from systemu.interface.ui_helpers import RepaintGate
        g = RepaintGate()
        g.should_paint()
        for _ in range(25):
            g.bump()
        assert g.should_paint() is True
        assert g.should_paint() is False


class TestOpenState:
    def test_record_accepts_event_args_and_raw_bool(self):
        from systemu.interface.ui_helpers import record_open_state
        state = {}

        class Args:  # NiceGUI ValueChangeEventArguments shape
            value = True

        record_open_state(state, 1, Args())
        record_open_state(state, 2, False)
        assert state == {1: True, 2: False}

    def test_prune_drops_evicted_keys(self):
        from systemu.interface.ui_helpers import prune_open_state
        state = {1: True, 2: False, 3: True}
        prune_open_state(state, [2, 3])
        assert state == {2: False, 3: True}


class TestPanesUseStatefulExpansion:
    """The two timer-refreshed panes must not rebuild collapsed expansions."""

    def test_stateful_expansion_restores_recorded_state(self):
        from systemu.interface import ui_helpers
        src = inspect.getsource(ui_helpers.stateful_expansion)
        assert "open_state.get(state_key" in src, \
            "rebuilds must restore the recorded open/closed state"
        assert "record_open_state" in src, \
            "user toggles must be recorded for the next rebuild"

    def test_live_events_pane_contract(self):
        from systemu.interface.components import live_events_pane as mod
        src = inspect.getsource(mod)
        assert "stateful_expansion(" in src
        assert "ui.expansion(header, value=False)" not in src, \
            "a bare value=False expansion is destroyed collapsed on every repaint"
        assert "gate.bump()" in src and "gate.should_paint()" in src, \
            "repaints must be change-gated or every tick destroys widget state"
        assert "prune_open_state(" in src, "state must not grow unboundedly"

    def test_right_rail_contract(self):
        from systemu.interface.components import right_rail as mod
        src = inspect.getsource(mod)
        assert "stateful_expansion(" in src
        assert "ui.expansion(header, value=False)" not in src
        assert "gate.bump()" in src and "gate.should_paint()" in src
        assert "safe_timer(0.5, _pane.refresh)" not in src, \
            "the tick must be gated, not an unconditional refresh"
        assert "prune_open_state(" in src
