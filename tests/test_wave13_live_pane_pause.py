"""W13.1 — the live pane respects the reader, and shows when work runs.

Field report (third time on this pane — operator must never see it again):
during ACTIVE streaming every event repaints the pane, so an expansion
opened mid-run can still get rebuilt under the reader and clicks racing
the rebuild can be eaten. Fix: while ANY expansion is open, repaints PAUSE
(a chip says so); closing resumes and applies queued updates.

Plus: a small spinner in the Live headers whenever background work
(jobs / executions) is running — the operator asked for a fingertip
indication that something is still happening.
"""
from __future__ import annotations

import inspect


class TestPauseWhileReading:
    def test_live_events_pane_pauses_repaints_when_open(self):
        from systemu.interface.components import live_events_pane as mod
        src = inspect.getsource(mod)
        assert "any(open_state.values())" in src, \
            "repaints must pause while the operator is reading an expansion"
        assert "paused" in src.lower()
        assert "gate.bump()" in src

    def test_right_rail_pauses_repaints_when_open(self):
        from systemu.interface.components import right_rail as mod
        src = inspect.getsource(mod)
        assert "any(open_state.values())" in src
        assert "paused" in src.lower()


class TestBackgroundActivity:
    def test_zero_when_nothing_available(self, monkeypatch):
        from systemu.interface import ui_helpers as uh
        import systemu.interface.jobs as jobs_mod
        import systemu.runtime.supervisor as sup_mod
        monkeypatch.setattr(jobs_mod.JobManager, "get",
                            classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError())))
        monkeypatch.setattr(sup_mod.Supervisor, "get",
                            classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError())))
        assert uh.background_activity_count() == 0

    def test_counts_jobs_and_running_executions(self, monkeypatch):
        from systemu.interface import ui_helpers as uh
        import systemu.interface.jobs as jobs_mod
        import systemu.runtime.supervisor as sup_mod

        class _JM:
            def get_active_jobs(self):
                return [1, 2]

        class _Sup:
            def get_status(self):
                return {"running_count": 1}

        monkeypatch.setattr(jobs_mod.JobManager, "get",
                            classmethod(lambda cls: _JM()))
        monkeypatch.setattr(sup_mod.Supervisor, "get",
                            classmethod(lambda cls: _Sup()))
        assert uh.background_activity_count() == 3

    def test_rail_live_header_has_the_spinner(self):
        from systemu.interface.components import right_rail as mod
        src = inspect.getsource(mod.render_right_rail)
        assert "ui.spinner" in src and "background_activity_count" in src

    def test_events_pane_has_the_spinner(self):
        from systemu.interface.components import live_events_pane as mod
        src = inspect.getsource(mod)
        assert "ui.spinner" in src and "background_activity_count" in src

    def test_rail_spinner_has_explicit_color(self):
        """v0.9.36: the busy dots MUST set an explicit color — the bare Quasar
        default is muted at size="sm" on the dark rail and reads as invisible
        (field report: "dots near the Live badge not visible")."""
        import re
        from systemu.interface.components import right_rail as mod
        src = inspect.getsource(mod.render_right_rail)
        assert re.search(r'ui\.spinner\([^)]*color=', src), \
            "Live-header dots spinner must pass an explicit color="

    def test_events_pane_spinner_has_explicit_color(self):
        import re
        from systemu.interface.components import live_events_pane as mod
        src = inspect.getsource(mod)
        assert re.search(r'ui\.spinner\([^)]*color=', src), \
            "live-events-pane dots spinner must pass an explicit color="
