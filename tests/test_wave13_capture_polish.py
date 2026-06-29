"""W13.4 — capture polish: guided handoff after Stop & Analyze, and the
"Run again" button (the repeatability payoff, made visible)."""
from __future__ import annotations

import inspect

import pytest


class TestStopHandoff:
    def test_stop_capture_guides_to_work(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard._stop_capture)
        assert 'navigate.to("/work")' in src, \
            "Stop & Analyze must take the operator where the workflow appears"
        assert "ring" in src.lower() or "ready to review" in src.lower()

    def test_worker_thread_reenters_the_client(self):
        from systemu.interface import dashboard
        src = inspect.getsource(dashboard._stop_capture)
        assert "ui.context.client" in src and "with _client" in src, \
            "W7.1: UI ops from the launcher thread need the captured client"


class TestRunAgain:
    def test_can_rerun_only_terminal_unapproved_rows(self):
        from systemu.interface.pages.work import can_rerun
        assert can_rerun({"status": "completed", "needs_approval": False})
        assert can_rerun({"status": "FAILED", "needs_approval": False})
        assert not can_rerun({"status": "completed", "needs_approval": True})
        assert not can_rerun({"status": "running", "needs_approval": False})
        assert not can_rerun({"status": "", "needs_approval": False})

    def test_rerun_submits_to_assigned_shadow(self, monkeypatch, tmp_path):
        from systemu.interface.pages import work
        import systemu.runtime.supervisor as sup_mod
        from systemu.interface import dashboard_state as ds

        calls = []

        class _Sup:
            def submit(self, activity_id, shadow_id, **kw):
                calls.append((activity_id, shadow_id, kw.get("reason")))
                return "sub_x"

        class _Vault:
            def load_index(self, kind):
                return [{"id": "activity_1", "scroll_id": "scroll_9",
                         "assigned_shadow_id": "shadow_7", "name": "Notes"}]

        class _State:
            vault = _Vault()

        monkeypatch.setattr(sup_mod.Supervisor, "get",
                            classmethod(lambda cls: _Sup()))
        monkeypatch.setattr(ds.AppState, "get",
                            classmethod(lambda cls: _State()))
        msg = work.rerun_workflow("scroll_9")
        assert calls == [("activity_1", "shadow_7", "operator_rerun")]
        assert "Notes" in msg

    def test_rerun_fails_plainly_without_shadow(self, monkeypatch):
        from systemu.interface.pages import work
        from systemu.interface import dashboard_state as ds

        class _Vault:
            def load_index(self, kind):
                return [{"id": "a", "scroll_id": "scroll_9",
                         "assigned_shadow_id": None}]

        class _State:
            vault = _Vault()

        monkeypatch.setattr(ds.AppState, "get",
                            classmethod(lambda cls: _State()))
        with pytest.raises(ValueError, match="approve"):
            work.rerun_workflow("scroll_9")

    def test_rerun_by_activity_id_resubmits(self, monkeypatch):
        # v0.9.51 live-events Re-trigger: the event context carries activity_id
        # (not scroll_id), so the button uses the by-activity path; it must look up
        # by id and submit with the activity's own scroll_id.
        from systemu.interface.pages import work
        import systemu.runtime.supervisor as sup_mod
        from systemu.interface import dashboard_state as ds

        calls = []

        class _Sup:
            def submit(self, activity_id, shadow_id, **kw):
                calls.append((activity_id, shadow_id, kw.get("reason"), kw.get("scroll_id")))
                return "sub_x"

        class _Vault:
            def load_index(self, kind):
                return [{"id": "act_2", "scroll_id": "scroll_5",
                         "assigned_shadow_id": "shadow_3", "name": "Zip job"}]

        class _State:
            vault = _Vault()

        monkeypatch.setattr(sup_mod.Supervisor, "get", classmethod(lambda cls: _Sup()))
        monkeypatch.setattr(ds.AppState, "get", classmethod(lambda cls: _State()))
        msg = work.rerun_workflow_by_activity("act_2")
        assert calls == [("act_2", "shadow_3", "operator_rerun", "scroll_5")]
        assert "Zip job" in msg

    def test_row_renders_the_button(self):
        from systemu.interface.pages import work
        src = inspect.getsource(work._render_row)
        assert "Run again" in src and "can_rerun(row)" in src