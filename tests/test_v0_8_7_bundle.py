"""v0.8.7 bundle regression tests."""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest


# ─── Subsystem 1: Remove execute dedup ─────────────────────────────────────

class TestNoDedup:
    def setup_method(self):
        from systemu.interface.jobs import JobManager
        JobManager._instance = None

    def test_start_job_does_not_dedup_on_same_key(self, monkeypatch):
        """Two start_job calls with the same dedup_key must yield two distinct jobs.

        v0.8.6 returned the existing job; v0.8.7 spawns a fresh one every time.
        """
        from systemu.interface.jobs import JobManager, JobStatus, Job
        jm = JobManager.get()

        # Stub Popen so no real subprocess spawns; stub Thread so the poll thread
        # doesn't immediately tear down our Jobs.
        monkeypatch.setattr("systemu.interface.jobs.subprocess.Popen", MagicMock())
        monkeypatch.setattr("systemu.interface.jobs.threading.Thread", MagicMock())

        job1 = jm.start_job(
            name="r1", job_type="execute",
            cmd=["python", "-c", "pass"], cwd="/tmp",
            dedup_key="execute:shadow_X:scroll_Y",
        )
        job2 = jm.start_job(
            name="r2", job_type="execute",
            cmd=["python", "-c", "pass"], cwd="/tmp",
            dedup_key="execute:shadow_X:scroll_Y",
        )
        assert job1.id != job2.id, (
            "start_job returned the existing job — dedup gate not removed"
        )
        assert job1.dedup_key == job2.dedup_key == "execute:shadow_X:scroll_Y"

    def test_find_active_by_dedup_key_still_works(self, monkeypatch):
        """find_active_by_dedup_key is kept as informational helper.

        It should still return active jobs so the UI can surface 'N other runs
        for this shadow+scroll are in flight'.
        """
        from systemu.interface.jobs import JobManager, JobStatus, Job
        from datetime import datetime
        jm = JobManager.get()

        # Pre-populate one RUNNING execute job
        jm.jobs["r1"] = Job(
            id="r1", name="r", type="execute",
            status=JobStatus.RUNNING, start_time=datetime.now(),
            dedup_key="execute:s:c",
        )
        found = jm.find_active_by_dedup_key("execute:s:c")
        assert found is not None
        assert found.id == "r1"

        # Unknown key returns None
        assert jm.find_active_by_dedup_key("execute:other:key") is None
        assert jm.find_active_by_dedup_key("") is None

    def test_scheduled_dispatch_fires_even_if_active_run_exists(self, tmp_path, monkeypatch):
        """A scheduled fire must dispatch even when a previous run of the same
        (shadow, scroll) is still active.

        v0.8.6 skipped the fire (still marked_fired). v0.8.7 fires regardless.
        """
        from systemu.scheduler.schedule_registry import create_schedule, get_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        config = MagicMock()
        config.vault_dir = str(tmp_path / "vault")

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        s = create_schedule(
            shadow_id="shadow_x", scroll_id="scroll_y",
            mode=ScheduleMode.ONCE, scheduled_at=past, vault=v,
        )

        sched_jobs._config = config
        sched_jobs._vault = v

        # Mock JobManager: find_active_by_dedup_key returns an active job
        # (would have triggered the skip in v0.8.6), but start_job MUST still
        # be called.
        running_job = MagicMock(id="job_existing", status=MagicMock(value="running"))
        fresh_job   = MagicMock(id="job_new",      status=MagicMock(value="running"))
        jm_spy = MagicMock()
        jm_spy.find_active_by_dedup_key = MagicMock(return_value=running_job)
        jm_spy.start_job = MagicMock(return_value=fresh_job)
        monkeypatch.setattr("systemu.interface.jobs.JobManager.get",
                            lambda: jm_spy)

        sched_jobs._scheduled_execute_job()

        # v0.8.7: start_job called despite find_active_by_dedup_key returning
        # a running job. v0.8.6 would have skipped.
        assert jm_spy.start_job.called, (
            "scheduled dispatch did NOT call start_job — v0.8.7 dedup-removal "
            "not applied"
        )
        # mark_fired was still called so the schedule advances (or completes)
        reloaded = get_schedule(s.id, v)
        assert reloaded.last_fire_at is not None


# ─── Subsystem 2 schema: Schedule new fields ───────────────────────────────

class TestScheduleSchema:
    def test_missed_field_defaults_false(self):
        from systemu.core.models import Schedule, ScheduleMode
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        s = Schedule(
            id="s1", shadow_id="sh", scroll_id="sc",
            mode=ScheduleMode.ONCE, scheduled_at=future,
            next_fire_at=future,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        assert s.missed is False

    def test_missed_fires_count_defaults_zero(self):
        from systemu.core.models import Schedule, ScheduleMode
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        s = Schedule(
            id="s2", shadow_id="sh", scroll_id="sc",
            mode=ScheduleMode.RECURRING, interval_minutes=30,
            scheduled_at=future, next_fire_at=future,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        assert s.missed_fires_count == 0

    def test_last_missed_at_defaults_none(self):
        from systemu.core.models import Schedule, ScheduleMode
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        s = Schedule(
            id="s3", shadow_id="sh", scroll_id="sc",
            mode=ScheduleMode.ONCE, scheduled_at=future,
            next_fire_at=future,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        assert s.last_missed_at is None

    def test_existing_v086_schedule_json_loads_without_migration(self):
        """A schedule JSON file written by v0.8.6 (without the new fields)
        must load cleanly into the v0.8.7 model. Pydantic uses field defaults
        for absent keys."""
        from systemu.core.models import Schedule
        v086_json = """
        {
            "id": "schedule_legacy",
            "shadow_id": "shadow_x",
            "scroll_id": "scroll_y",
            "mode": "once",
            "dry_run": false,
            "scheduled_at": "2026-12-31T09:00:00",
            "interval_minutes": null,
            "end_at": null,
            "next_fire_at": "2026-12-31T09:00:00",
            "last_fire_at": null,
            "status": "active",
            "created_at": "2026-05-29T10:00:00",
            "created_by": "operator (dashboard)"
        }
        """
        s = Schedule.model_validate_json(v086_json)
        assert s.id == "schedule_legacy"
        assert s.missed is False
        assert s.missed_fires_count == 0
        assert s.last_missed_at is None


# ─── Subsystem 2 registry: mark_missed ─────────────────────────────────────

class TestMarkMissed:
    def _make_vault(self, tmp_path):
        v = MagicMock()
        v.root = str(tmp_path)
        return v

    def test_mark_missed_once_completes_with_missed_flag(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule, mark_missed, get_schedule
        from systemu.core.models import ScheduleMode, ScheduleStatus
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.ONCE, scheduled_at=past, vault=v,
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        mark_missed(s.id, now, v, advance_to=None)
        reloaded = get_schedule(s.id, v)
        assert reloaded.status == ScheduleStatus.COMPLETED
        assert reloaded.missed is True
        assert reloaded.last_missed_at == now

    def test_mark_missed_recurring_advances_and_increments_count(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule, mark_missed, get_schedule
        from systemu.core.models import ScheduleMode, ScheduleStatus
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.RECURRING, interval_minutes=30,
            scheduled_at=past, vault=v,
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        next_fire = now + timedelta(minutes=15)
        mark_missed(s.id, now, v, advance_to=next_fire)
        reloaded = get_schedule(s.id, v)
        assert reloaded.status == ScheduleStatus.ACTIVE
        assert reloaded.missed_fires_count == 1
        assert reloaded.next_fire_at == next_fire
        assert reloaded.last_missed_at == now

    def test_mark_missed_recurring_completes_when_past_end_at(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule, mark_missed, get_schedule
        from systemu.core.models import ScheduleMode, ScheduleStatus
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=10)
        end = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.RECURRING, interval_minutes=60,
            scheduled_at=past, end_at=end, vault=v,
        )
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        next_fire = now + timedelta(minutes=30)   # past end_at
        mark_missed(s.id, now, v, advance_to=next_fire)
        reloaded = get_schedule(s.id, v)
        assert reloaded.status == ScheduleStatus.COMPLETED
        # missed_fires_count was still incremented
        assert reloaded.missed_fires_count == 1


# ─── Subsystem 2 detection: _scheduled_execute_job staleness ───────────────

class TestMissedScheduleDetection:
    def test_threshold_constant_default_300s(self):
        from systemu.scheduler.jobs import SCHEDULE_MISSED_THRESHOLD_SECONDS
        assert SCHEDULE_MISSED_THRESHOLD_SECONDS == 300

    def test_fresh_late_fire_within_threshold_still_dispatches(self, tmp_path, monkeypatch):
        """Schedule 2 min late (under 300s threshold) → fires normally."""
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        config = MagicMock()
        config.vault_dir = str(tmp_path / "vault")

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.ONCE, scheduled_at=past, vault=v,
        )
        sched_jobs._config = config
        sched_jobs._vault = v

        jm_spy = MagicMock()
        jm_spy.find_active_by_dedup_key = MagicMock(return_value=None)
        jm_spy.start_job = MagicMock(return_value=MagicMock(id="j1", status=MagicMock(value="running")))
        monkeypatch.setattr("systemu.interface.jobs.JobManager.get",
                            lambda: jm_spy)

        # Stub the missed-handler to detect if it's wrongly called
        missed_spy = MagicMock()
        monkeypatch.setattr(sched_jobs, "_handle_missed_schedule", missed_spy)

        sched_jobs._scheduled_execute_job()

        # Dispatched (fresh path), NOT missed
        assert jm_spy.start_job.called
        missed_spy.assert_not_called()

    def test_stale_fire_beyond_threshold_triggers_missed_handling(self, tmp_path, monkeypatch):
        """Schedule 10 min late (over 300s threshold) → goes to missed path."""
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        config = MagicMock()
        config.vault_dir = str(tmp_path / "vault")

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.ONCE, scheduled_at=past, vault=v,
        )
        sched_jobs._config = config
        sched_jobs._vault = v

        jm_spy = MagicMock()
        jm_spy.find_active_by_dedup_key = MagicMock(return_value=None)
        jm_spy.start_job = MagicMock()
        monkeypatch.setattr("systemu.interface.jobs.JobManager.get",
                            lambda: jm_spy)

        missed_spy = MagicMock()
        monkeypatch.setattr(sched_jobs, "_handle_missed_schedule", missed_spy)

        sched_jobs._scheduled_execute_job()

        # NOT dispatched (stale → missed)
        jm_spy.start_job.assert_not_called()
        assert missed_spy.called
        # Called with the schedule, now (datetime), age (~600s), config, vault
        args = missed_spy.call_args.args
        assert args[0].id == s.id
        assert args[2] > 300   # age_seconds

    def test_recurring_missed_recomputes_next_fire_to_future(self, tmp_path, monkeypatch):
        """Recurring schedule with scheduled_at=09:00, interval=60min, now=14:30:
           next_fire_at should advance to 15:00 (smallest scheduled_at + N*interval > now)."""
        from systemu.scheduler.schedule_registry import create_schedule, get_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        config = MagicMock()

        # Fixed "now" for deterministic computation
        now = datetime(2026, 6, 1, 14, 30, 0)
        nine_am = datetime(2026, 6, 1, 9, 0, 0)

        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.RECURRING, interval_minutes=60,
            scheduled_at=nine_am, vault=v,
        )

        # Test _compute_next_valid_fire directly
        next_fire = sched_jobs._compute_next_valid_fire(s, now)
        assert next_fire == datetime(2026, 6, 1, 15, 0, 0), (
            f"expected 15:00, got {next_fire}"
        )

    def test_recurring_missed_increments_count(self, tmp_path, monkeypatch):
        from systemu.scheduler.schedule_registry import create_schedule, get_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        config = MagicMock()

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.RECURRING, interval_minutes=30,
            scheduled_at=past, vault=v,
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        age = 7200.0   # 2 hours
        # Stub the notification + event helpers so we don't depend on EventBus etc.
        monkeypatch.setattr(sched_jobs, "_queue_missed_notification", MagicMock())
        monkeypatch.setattr(sched_jobs, "_publish_missed_event", MagicMock())
        sched_jobs._handle_missed_schedule(s, now, age, config, v)

        reloaded = get_schedule(s.id, v)
        assert reloaded.missed_fires_count == 1
        assert reloaded.next_fire_at > now

    def test_once_missed_marks_completed_with_missed_flag(self, tmp_path, monkeypatch):
        from systemu.scheduler.schedule_registry import create_schedule, get_schedule
        from systemu.core.models import ScheduleMode, ScheduleStatus
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        config = MagicMock()

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.ONCE, scheduled_at=past, vault=v,
        )

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        monkeypatch.setattr(sched_jobs, "_queue_missed_notification", MagicMock())
        monkeypatch.setattr(sched_jobs, "_publish_missed_event", MagicMock())
        sched_jobs._handle_missed_schedule(s, now, 7200.0, config, v)

        reloaded = get_schedule(s.id, v)
        assert reloaded.status == ScheduleStatus.COMPLETED
        assert reloaded.missed is True

    def test_format_age_basic(self):
        from systemu.scheduler.jobs import _format_age
        assert _format_age(30) == "30s"
        assert _format_age(90) == "1m 30s"
        assert _format_age(3600) == "1h"
        assert _format_age(3660) == "1h 1m"
        assert _format_age(86400) == "1d"
        assert _format_age(90061) == "1d 1h"


# ─── Subsystem 2 alerts: end-to-end notification + event ───────────────────

class TestMissedScheduleAlerts:
    def test_missed_queues_notification_with_schedule_missed_type(self, tmp_path, monkeypatch):
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        # Make vault.get_shadow / get_scroll return objects with .name
        v.get_shadow = MagicMock(return_value=MagicMock(name="TimeCapture"))
        v.get_scroll = MagicMock(return_value=MagicMock(name="CET Time"))
        # vault.queue_notification captures the call
        v.queue_notification = MagicMock()

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        s = create_schedule(
            shadow_id="shadow_t", scroll_id="scroll_c",
            mode=ScheduleMode.ONCE, scheduled_at=past, vault=v,
        )

        # Run the per-schedule missed handler directly
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        sched_jobs._queue_missed_notification(s, 7200.0, advanced_to=None, vault=v)

        # Was a Notification queued? Verify the type.
        assert v.queue_notification.called
        notif = v.queue_notification.call_args.args[0]
        assert notif.context["notification_type"] == "schedule_missed"
        assert notif.context["schedule_id"] == s.id
        assert notif.actions == ["OK"]
        assert "missed" in notif.title.lower()

    def test_missed_publishes_warning_event(self, tmp_path, monkeypatch):
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from systemu.interface.event_bus import EventBus
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.ONCE, scheduled_at=past, vault=v,
        )

        # Reset and subscribe to capture
        EventBus._instance = None
        received = []
        EventBus.get().subscribe(lambda e: received.append(e), replay=False)

        sched_jobs._publish_missed_event(s, 3600.0, advanced_to=None)

        # At least one event with category=scheduler, level=WARNING
        events = [e for e in received
                  if e.get("category") == "scheduler" and e.get("level") == "WARNING"]
        assert len(events) >= 1
        assert s.id in events[-1]["message"]
        assert events[-1]["context"]["schedule_id"] == s.id
