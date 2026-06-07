"""v0.9.6 L7 inactivity-triggered curator state + idle check tests."""
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import pytest

from sharing_on.config import Config


class TestConfigCuratorFields:
    _KEYS = (
        "SYSTEMU_CURATOR_ENABLED",
        "SYSTEMU_CURATOR_INTERVAL_HOURS",
        "SYSTEMU_CURATOR_MIN_IDLE_MINUTES",
    )

    def test_defaults(self, monkeypatch):
        for k in self._KEYS:
            monkeypatch.delenv(k, raising=False)
        cfg = Config()
        assert cfg.curator_enabled is True
        assert cfg.curator_interval_hours == 168  # 7 days
        assert cfg.curator_min_idle_minutes == 120  # 2 hours

    def test_env_overrides(self):
        env = {
            "SYSTEMU_CURATOR_ENABLED": "false",
            "SYSTEMU_CURATOR_INTERVAL_HOURS": "24",
            "SYSTEMU_CURATOR_MIN_IDLE_MINUTES": "30",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = Config.from_env()
        assert cfg.curator_enabled is False
        assert cfg.curator_interval_hours == 24
        assert cfg.curator_min_idle_minutes == 30


class TestCuratorDaemonWiring:
    """v0.9.6 regression guard: the curator must be CALLED by the daemon, not
    just exist as a module.

    Before this guard, curator.should_run()/mark_run_complete() had zero
    production call sites — the module was green in unit tests but never ran.
    These tests pin the production wiring via source inspection + a behavioural
    check on the job function, so a future refactor that drops the call site
    fails loudly.
    """

    def test_jobs_module_exposes_curator_review_job(self):
        from systemu.scheduler import jobs
        assert hasattr(jobs, "curator_review_job"), (
            "jobs.py must define curator_review_job (the daemon-scheduled "
            "entry point that gates on curator.should_run())."
        )

    def test_curator_review_job_calls_should_run_and_mark_complete(self):
        import inspect
        from systemu.scheduler import jobs
        src = inspect.getsource(jobs.curator_review_job)
        assert "curator.should_run" in src, (
            "curator_review_job must gate on curator.should_run()"
        )
        assert "mark_run_complete" in src, (
            "curator_review_job must record the run via curator.mark_run_complete()"
        )

    def test_daemon_registers_curator_review_job(self):
        import inspect
        from systemu.scheduler import daemon
        src = inspect.getsource(daemon)
        assert "curator_review_job" in src, (
            "daemon.py must import + register curator_review_job as a scheduled "
            "job — otherwise the curator never runs in production."
        )
        assert 'id="curator_review"' in src, (
            "daemon.py must register the curator job with a stable id."
        )

    def test_curator_review_job_runs_pass_when_due(self, tmp_path, monkeypatch):
        """Behavioural wiring proof: when should_run() is True, the job calls
        the consolidation pass and records completion."""
        from systemu.scheduler import jobs
        from systemu.runtime import curator

        calls = {"consolidated": 0, "marked": False}

        class _Cfg:
            vault_dir = str(tmp_path)
            curator_enabled = True
            curator_interval_hours = 168

        # Force should_run True, stub the heavy pass + the state write.
        monkeypatch.setattr(curator, "should_run", lambda root, cfg: True)
        monkeypatch.setattr(
            jobs, "run_consolidation_for_all",
            lambda config, vault: calls.__setitem__("consolidated", 3) or 3,
        )
        monkeypatch.setattr(
            curator, "mark_run_complete",
            lambda root, **kw: calls.__setitem__("marked", True),
        )
        monkeypatch.setattr(jobs, "_config", _Cfg())
        monkeypatch.setattr(jobs, "_vault", object())

        jobs.curator_review_job()
        assert calls["consolidated"] == 3, "should have run the lifecycle pass"
        assert calls["marked"] is True, "should have recorded the run"

    def test_curator_review_job_skips_when_not_due(self, tmp_path, monkeypatch):
        from systemu.scheduler import jobs
        from systemu.runtime import curator
        ran = {"pass": False}

        class _Cfg:
            vault_dir = str(tmp_path)
            curator_enabled = True
            curator_interval_hours = 168

        monkeypatch.setattr(curator, "should_run", lambda root, cfg: False)
        monkeypatch.setattr(
            jobs, "run_consolidation_for_all",
            lambda config, vault: ran.__setitem__("pass", True) or 0,
        )
        monkeypatch.setattr(jobs, "_config", _Cfg())
        monkeypatch.setattr(jobs, "_vault", object())

        jobs.curator_review_job()
        assert ran["pass"] is False, "must NOT run the pass when not due"


class TestCuratorState:
    def test_load_state_creates_default_when_missing(self, tmp_path):
        from systemu.runtime.curator import load_state, default_state
        state = load_state(tmp_path)
        # Default state shape
        assert state == default_state()
        assert state["last_run_at"] is None
        assert state["paused"] is False
        assert state["run_count"] == 0

    def test_save_then_load_state(self, tmp_path):
        from systemu.runtime.curator import load_state, save_state
        s = {
            "last_run_at": "2026-06-07T12:00:00Z",
            "last_run_summary": "demo run",
            "paused": False,
            "run_count": 1,
        }
        save_state(tmp_path, s)
        rebuilt = load_state(tmp_path)
        assert rebuilt["last_run_at"] == "2026-06-07T12:00:00Z"
        assert rebuilt["run_count"] == 1

    def test_state_file_is_jsonl_or_json(self, tmp_path):
        from systemu.runtime.curator import save_state, _state_file
        save_state(tmp_path, {"last_run_at": None, "paused": False, "run_count": 0})
        p = _state_file(tmp_path)
        assert p.exists()
        content = p.read_text(encoding="utf-8")
        assert "{" in content


class TestShouldRun:
    def test_first_run_when_no_history(self, tmp_path):
        from systemu.runtime.curator import should_run
        cfg = Config()
        # No prior state -> should run (first time)
        assert should_run(tmp_path, cfg) is True

    def test_skips_when_disabled(self, tmp_path):
        from systemu.runtime.curator import should_run
        cfg = Config()
        cfg.curator_enabled = False
        assert should_run(tmp_path, cfg) is False

    def test_skips_when_paused(self, tmp_path):
        from systemu.runtime.curator import save_state, should_run
        save_state(tmp_path, {"last_run_at": None, "paused": True, "run_count": 0})
        cfg = Config()
        assert should_run(tmp_path, cfg) is False

    def test_skips_when_within_interval(self, tmp_path):
        from systemu.runtime.curator import save_state, should_run
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        save_state(tmp_path, {"last_run_at": recent, "paused": False, "run_count": 1})
        cfg = Config()
        cfg.curator_interval_hours = 24  # Need 24h gap, only 1h has passed
        assert should_run(tmp_path, cfg) is False

    def test_runs_when_interval_elapsed(self, tmp_path):
        from systemu.runtime.curator import save_state, should_run
        old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        save_state(tmp_path, {"last_run_at": old, "paused": False, "run_count": 1})
        cfg = Config()
        cfg.curator_interval_hours = 24
        assert should_run(tmp_path, cfg) is True


class TestRunMarkers:
    def test_mark_run_updates_state(self, tmp_path):
        from systemu.runtime.curator import mark_run_complete, load_state
        mark_run_complete(tmp_path, summary="reviewed 5 skills, archived 2")
        state = load_state(tmp_path)
        assert state["last_run_at"] is not None
        assert state["run_count"] == 1
        assert "5 skills" in state["last_run_summary"]

    def test_mark_run_increments_count(self, tmp_path):
        from systemu.runtime.curator import mark_run_complete, load_state
        mark_run_complete(tmp_path, summary="r1")
        mark_run_complete(tmp_path, summary="r2")
        mark_run_complete(tmp_path, summary="r3")
        state = load_state(tmp_path)
        assert state["run_count"] == 3
