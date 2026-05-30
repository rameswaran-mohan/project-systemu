"""v0.8.6 bundle regression tests."""
from __future__ import annotations
from unittest.mock import MagicMock, patch
import pytest


# ─── Subsystem 1: JobManager queue + dedup ─────────────────────────────────

class TestJobManagerQueue:
    def setup_method(self):
        from systemu.interface.jobs import JobManager
        # Each test gets a fresh JobManager
        JobManager._instance = None

    def test_queued_status_exists_in_enum(self):
        from systemu.interface.jobs import JobStatus
        assert hasattr(JobStatus, "QUEUED")
        assert JobStatus.QUEUED.value == "queued"

    def test_job_dataclass_has_dedup_key_field(self):
        from systemu.interface.jobs import Job, JobStatus
        j = Job(id="abc", name="t", type="execute", status=JobStatus.RUNNING)
        assert hasattr(j, "dedup_key")
        assert j.dedup_key == ""   # default empty

    def test_job_dataclass_dedup_key_accepts_value(self):
        from systemu.interface.jobs import Job, JobStatus
        j = Job(id="abc", name="t", type="execute", status=JobStatus.RUNNING,
                dedup_key="execute:shadow_x:scroll_y")
        assert j.dedup_key == "execute:shadow_x:scroll_y"

    def test_execute_at_cap_enqueues_instead_of_spawning(self, monkeypatch):
        """When 3 execute jobs are RUNNING, the 4th must enter QUEUED state."""
        from systemu.interface.jobs import JobManager, JobStatus, Job
        from datetime import datetime
        jm = JobManager.get()

        # Pre-populate 3 RUNNING execute jobs (simulate cap reached)
        for i in range(3):
            j = Job(
                id=f"run{i}", name=f"r{i}", type="execute",
                status=JobStatus.RUNNING, start_time=datetime.now(),
            )
            jm.jobs[j.id] = j

        # Patch subprocess.Popen so a real spawn doesn't happen if cap check fails
        monkeypatch.setattr("systemu.interface.jobs.subprocess.Popen",
                            MagicMock())

        result = jm.start_job(
            name="r4", job_type="execute",
            cmd=["python", "-c", "pass"],
            cwd="/tmp",
        )
        assert result.status == JobStatus.QUEUED, (
            f"expected QUEUED, got {result.status}"
        )
        assert result.id in jm.jobs
        assert any(p.id == result.id for p in jm._execute_pending)

    def test_non_execute_job_never_enqueues(self, monkeypatch):
        """Capture / forge / other types must spawn immediately even past cap."""
        from systemu.interface.jobs import JobManager, JobStatus, Job
        from datetime import datetime
        jm = JobManager.get()

        for i in range(5):
            j = Job(id=f"e{i}", name="e", type="execute",
                    status=JobStatus.RUNNING, start_time=datetime.now())
            jm.jobs[j.id] = j

        popen_spy = MagicMock()
        monkeypatch.setattr("systemu.interface.jobs.subprocess.Popen", popen_spy)
        monkeypatch.setattr("systemu.interface.jobs.threading.Thread",
                            MagicMock())

        result = jm.start_job(
            name="cap", job_type="capture",
            cmd=["python"], cwd="/tmp",
        )
        assert result.status == JobStatus.RUNNING
        assert popen_spy.called   # spawned immediately

    def test_queue_depth_cap_raises(self, monkeypatch):
        """Submitting past JOBMANAGER_MAX_EXECUTE_QUEUE_DEPTH raises RuntimeError."""
        import os
        from systemu.interface.jobs import JobManager, JobStatus, Job
        from datetime import datetime

        monkeypatch.setenv("JOBMANAGER_MAX_EXECUTE_QUEUE_DEPTH", "5")
        # Reload the module so the new env var is picked up
        import importlib
        from systemu.interface import jobs as jobs_mod
        importlib.reload(jobs_mod)
        from systemu.interface.jobs import JobManager
        JobManager._instance = None
        jm = JobManager.get()

        # 3 RUNNING + 2 QUEUED = 5 total. 6th should reject.
        for i in range(3):
            jm.jobs[f"r{i}"] = jobs_mod.Job(
                id=f"r{i}", name="r", type="execute",
                status=jobs_mod.JobStatus.RUNNING,
                start_time=datetime.now(),
            )
        for i in range(2):
            qj = jobs_mod.Job(
                id=f"q{i}", name="q", type="execute",
                status=jobs_mod.JobStatus.QUEUED,
                start_time=datetime.now(),
            )
            jm.jobs[qj.id] = qj
            jm._execute_pending.append(qj)

        with pytest.raises(RuntimeError, match="queue full"):
            jm.start_job(
                name="overflow", job_type="execute",
                cmd=["python"], cwd="/tmp",
            )

    def test_dispatcher_promotes_queued_when_slot_frees(self, monkeypatch):
        """When a RUNNING execute finishes, dispatcher must promote oldest QUEUED."""
        from systemu.interface.jobs import JobManager, JobStatus, Job
        from datetime import datetime

        jm = JobManager.get()

        # Setup: 3 running, 1 queued
        for i in range(3):
            jm.jobs[f"r{i}"] = Job(
                id=f"r{i}", name="r", type="execute",
                status=JobStatus.RUNNING, start_time=datetime.now(),
            )
        qj = Job(
            id="q1", name="queued1", type="execute",
            status=JobStatus.QUEUED, start_time=datetime.now(),
        )
        jm.jobs["q1"] = qj
        jm._execute_pending.append(qj)

        # Stub _spawn_job to track calls (don't actually Popen)
        spawn_spy = MagicMock()
        spawn_spy.return_value = MagicMock(id="q1", status=JobStatus.RUNNING)
        monkeypatch.setattr(jm, "_spawn_job", spawn_spy)

        # Free a slot
        jm.jobs["r0"].status = JobStatus.COMPLETED

        # Manually invoke one dispatcher iteration
        jm._dispatcher_tick()

        # Verify spawn was called with the queued job's params and the job
        # got popped from the pending deque
        assert spawn_spy.called, "dispatcher did not spawn the queued job"
        assert qj not in jm._execute_pending

    def test_cancel_queued_removes_without_spawning(self, monkeypatch):
        from systemu.interface.jobs import JobManager, JobStatus, Job
        from datetime import datetime
        jm = JobManager.get()

        qj = Job(id="q1", name="q", type="execute",
                 status=JobStatus.QUEUED, start_time=datetime.now())
        jm.jobs["q1"] = qj
        jm._execute_pending.append(qj)

        spawn_spy = MagicMock()
        monkeypatch.setattr(jm, "_spawn_job", spawn_spy)

        result = jm.cancel_queued("q1")
        assert result is True
        assert jm.jobs["q1"].status == JobStatus.CANCELLED
        assert qj not in jm._execute_pending
        spawn_spy.assert_not_called()

    def test_cancel_queued_returns_false_for_unknown_or_running(self):
        from systemu.interface.jobs import JobManager, JobStatus, Job
        from datetime import datetime
        jm = JobManager.get()

        # Unknown id
        assert jm.cancel_queued("nope") is False

        # RUNNING job — should not be cancelled via cancel_queued
        rj = Job(id="r1", name="r", type="execute",
                 status=JobStatus.RUNNING, start_time=datetime.now())
        jm.jobs["r1"] = rj
        assert jm.cancel_queued("r1") is False
        assert rj.status == JobStatus.RUNNING


# ─── Subsystem 2: Schedule model ───────────────────────────────────────────

class TestScheduleModel:
    def test_once_schedule_validates(self):
        from systemu.core.models import Schedule, ScheduleMode, ScheduleStatus
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        s = Schedule(
            id="schedule_abc",
            shadow_id="shadow_x",
            scroll_id="scroll_y",
            mode=ScheduleMode.ONCE,
            scheduled_at=future,
            next_fire_at=future,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        assert s.mode == ScheduleMode.ONCE
        assert s.status == ScheduleStatus.ACTIVE
        assert s.interval_minutes is None
        assert s.dry_run is False

    def test_recurring_schedule_validates(self):
        from systemu.core.models import Schedule, ScheduleMode
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        s = Schedule(
            id="schedule_rec", shadow_id="s", scroll_id="sc",
            mode=ScheduleMode.RECURRING,
            interval_minutes=30,
            scheduled_at=future,
            next_fire_at=future,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        assert s.interval_minutes == 30
        assert s.end_at is None

    def test_status_enum_values(self):
        from systemu.core.models import ScheduleStatus
        assert ScheduleStatus.ACTIVE.value == "active"
        assert ScheduleStatus.COMPLETED.value == "completed"
        assert ScheduleStatus.CANCELLED.value == "cancelled"

    def test_mode_enum_values(self):
        from systemu.core.models import ScheduleMode
        assert ScheduleMode.ONCE.value == "once"
        assert ScheduleMode.RECURRING.value == "recurring"


# ─── Subsystem 3: Schedule registry ────────────────────────────────────────

class TestScheduleRegistry:
    def _make_vault(self, tmp_path):
        """Return a vault stub whose root is tmp_path."""
        from unittest.mock import MagicMock
        v = MagicMock()
        v.root = str(tmp_path)
        return v

    def test_create_once_schedule(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        sched = create_schedule(
            shadow_id="shadow_x", scroll_id="scroll_y",
            mode=ScheduleMode.ONCE, dry_run=False,
            scheduled_at=future, vault=v,
        )
        assert sched.mode == ScheduleMode.ONCE
        assert sched.next_fire_at == future
        # File created
        from pathlib import Path
        sched_dir = Path(tmp_path) / "schedules"
        files = list(sched_dir.glob("schedule_*.json"))
        assert len(files) == 1

    def test_create_recurring_requires_interval(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        with pytest.raises(ValueError, match="interval_minutes required"):
            create_schedule(
                shadow_id="s", scroll_id="sc",
                mode=ScheduleMode.RECURRING, scheduled_at=future,
                vault=v,
            )

    def test_create_recurring_min_interval_5min(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        with pytest.raises(ValueError, match="at least 5"):
            create_schedule(
                shadow_id="s", scroll_id="sc",
                mode=ScheduleMode.RECURRING, interval_minutes=3,
                scheduled_at=future, vault=v,
            )

    def test_list_active_returns_only_active(self, tmp_path):
        from systemu.scheduler.schedule_registry import (
            create_schedule, list_active_schedules, cancel_schedule,
        )
        from systemu.core.models import ScheduleMode
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        s1 = create_schedule(
            shadow_id="a", scroll_id="b",
            mode=ScheduleMode.ONCE, scheduled_at=future, vault=v,
        )
        s2 = create_schedule(
            shadow_id="c", scroll_id="d",
            mode=ScheduleMode.ONCE, scheduled_at=future, vault=v,
        )
        cancel_schedule(s1.id, v)

        active = list_active_schedules(v)
        ids = [s.id for s in active]
        assert s2.id in ids
        assert s1.id not in ids

    def test_mark_fired_once_completes(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule, mark_fired, get_schedule
        from systemu.core.models import ScheduleMode, ScheduleStatus
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.ONCE, scheduled_at=future, vault=v,
        )
        mark_fired(s.id, datetime.now(timezone.utc).replace(tzinfo=None), v)
        reloaded = get_schedule(s.id, v)
        assert reloaded.status == ScheduleStatus.COMPLETED

    def test_mark_fired_recurring_advances_next_fire(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule, mark_fired, get_schedule
        from systemu.core.models import ScheduleMode, ScheduleStatus
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.RECURRING, interval_minutes=30,
            scheduled_at=future, vault=v,
        )
        fire_time = datetime.now(timezone.utc).replace(tzinfo=None)
        mark_fired(s.id, fire_time, v)
        reloaded = get_schedule(s.id, v)
        assert reloaded.status == ScheduleStatus.ACTIVE
        # Skip-missed: next_fire = now + interval, NOT scheduled_at + interval
        expected_next = fire_time + timedelta(minutes=30)
        assert abs((reloaded.next_fire_at - expected_next).total_seconds()) < 2

    def test_cancel_schedule(self, tmp_path):
        from systemu.scheduler.schedule_registry import create_schedule, cancel_schedule, get_schedule
        from systemu.core.models import ScheduleMode, ScheduleStatus
        from datetime import datetime, timedelta, timezone

        v = self._make_vault(tmp_path)
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.ONCE, scheduled_at=future, vault=v,
        )
        assert cancel_schedule(s.id, v) is True
        reloaded = get_schedule(s.id, v)
        assert reloaded.status == ScheduleStatus.CANCELLED


# ─── Subsystem 4: scheduler hook ───────────────────────────────────────────

class TestScheduledExecuteJob:
    def test_due_schedule_dispatches_to_jobmanager(self, tmp_path, monkeypatch):
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        # Build a fake vault rooted at tmp_path
        v = MagicMock()
        v.root = str(tmp_path)

        config = MagicMock()
        config.vault_dir = str(tmp_path / "vault")

        # Create a schedule that is freshly due — 1 min ago, well under the
        # v0.8.7 staleness threshold (300s). 5 min lands exactly on the 300s
        # boundary, so a 15ms clock-tick crossing between the test's now() and
        # the scheduler's now() flips it between "due" and "missed" → flaky.
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        s = create_schedule(
            shadow_id="shadow_x", scroll_id="scroll_y",
            mode=ScheduleMode.ONCE, scheduled_at=past, vault=v,
        )

        # Inject _config + _vault into the jobs module
        sched_jobs._config = config
        sched_jobs._vault = v

        # Spy JobManager.start_job
        jm_spy = MagicMock()
        jm_spy.start_job = MagicMock(return_value=MagicMock(id="job1", status=MagicMock(value="running")))
        jm_spy.find_active_by_dedup_key = MagicMock(return_value=None)
        monkeypatch.setattr("systemu.interface.jobs.JobManager.get",
                            lambda: jm_spy)

        sched_jobs._scheduled_execute_job()

        assert jm_spy.start_job.called
        called_args = jm_spy.start_job.call_args
        assert called_args.kwargs.get("dedup_key") == "execute:shadow_x:scroll_y"

    def test_recurring_advances_after_fire(self, tmp_path, monkeypatch):
        from systemu.scheduler.schedule_registry import create_schedule, get_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        config = MagicMock()

        # v0.8.7: keep this well under SCHEDULE_MISSED_THRESHOLD_SECONDS (300s)
        # so the schedule goes through the FRESH dispatch path, not the missed
        # path. The original v0.8.6 value of 5 minutes is exactly on the
        # boundary and collides with the new staleness threshold under any
        # test-execution overhead.
        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.RECURRING, interval_minutes=30,
            scheduled_at=past, vault=v,
        )

        sched_jobs._config = config
        sched_jobs._vault = v
        jm_spy = MagicMock()
        jm_spy.find_active_by_dedup_key = MagicMock(return_value=None)
        jm_spy.start_job = MagicMock(return_value=MagicMock(id="job1", status=MagicMock(value="running")))
        monkeypatch.setattr("systemu.interface.jobs.JobManager.get",
                            lambda: jm_spy)

        sched_jobs._scheduled_execute_job()

        reloaded = get_schedule(s.id, v)
        # last_fire_at is set; next_fire_at advanced
        assert reloaded.last_fire_at is not None
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expected_next = now + timedelta(minutes=30)
        assert abs((reloaded.next_fire_at - expected_next).total_seconds()) < 5

    @pytest.mark.xfail(
        reason="v0.8.7 removed the dedup skip from _dispatch_scheduled — "
               "scheduled fires now dispatch even with active runs in flight",
        strict=True,
    )
    def test_skip_if_previous_run_still_active(self, tmp_path, monkeypatch):
        from systemu.scheduler.schedule_registry import create_schedule, get_schedule
        from systemu.core.models import ScheduleMode
        from systemu.scheduler import jobs as sched_jobs
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        config = MagicMock()

        past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        s = create_schedule(
            shadow_id="x", scroll_id="y",
            mode=ScheduleMode.RECURRING, interval_minutes=30,
            scheduled_at=past, vault=v,
        )

        sched_jobs._config = config
        sched_jobs._vault = v

        running_job = MagicMock(id="job_existing", status=MagicMock(value="running"))
        jm_spy = MagicMock()
        jm_spy.find_active_by_dedup_key = MagicMock(return_value=running_job)
        jm_spy.start_job = MagicMock()
        monkeypatch.setattr("systemu.interface.jobs.JobManager.get",
                            lambda: jm_spy)

        sched_jobs._scheduled_execute_job()

        # Did NOT call start_job (previous run blocking)
        jm_spy.start_job.assert_not_called()
        # Schedule was still mark_fired so we don't bombard with retries
        reloaded = get_schedule(s.id, v)
        assert reloaded.last_fire_at is not None


# ─── Subsystem 5: EventBus bridge ──────────────────────────────────────────

class TestEventBridgeWriter:
    def test_install_subscribes_and_writes_lines(self, tmp_path):
        from systemu.interface.event_bridge_writer import install_bridge_writer
        from systemu.interface.event_bus import EventBus
        import json

        bridge_path = str(tmp_path / "manual_events.jsonl")
        # Fresh EventBus
        EventBus._instance = None

        install_bridge_writer(bridge_path)
        EventBus.get().publish({"category": "shadow", "message": "hello"})

        # Wait for write (publish is synchronous)
        from pathlib import Path
        content = Path(bridge_path).read_text(encoding="utf-8")
        lines = [l for l in content.splitlines() if l.strip()]
        assert len(lines) >= 1
        last = json.loads(lines[-1])
        assert last["category"] == "shadow"
        assert last["message"] == "hello"

    def test_write_failure_does_not_raise(self, tmp_path, monkeypatch):
        from systemu.interface.event_bridge_writer import install_bridge_writer
        from systemu.interface.event_bus import EventBus

        EventBus._instance = None
        # Path that can never be written (read-only parent that doesn't exist)
        bad_path = "/nonexistent/path/manual_events.jsonl"
        install_bridge_writer(bad_path)

        # Must not raise
        EventBus.get().publish({"category": "shadow", "message": "x"})


class TestManualEventBridge:
    def test_tail_republishes_new_lines(self, tmp_path):
        """ManualEventBridge must read new lines and re-publish to EventBus."""
        from systemu.interface.manual_event_bridge import ManualEventBridge
        from systemu.interface.event_bus import EventBus
        import json, time

        EventBus._instance = None
        # Reset bridge singleton
        ManualEventBridge._instance = None

        bridge = ManualEventBridge.start(str(tmp_path))

        received: list = []
        EventBus.get().subscribe(lambda e: received.append(e), replay=False)

        # Append a line to the bridge file
        bridge_file = str(tmp_path / "manual_events.jsonl")
        with open(bridge_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"category": "shadow", "message": "from-subprocess"}) + "\n")

        # Wait up to 3s for tailer to pick it up (tail interval is 1s)
        for _ in range(30):
            if any(e.get("message") == "from-subprocess" for e in received):
                break
            time.sleep(0.1)

        bridge._stop_event.set()  # stop the thread
        assert any(e.get("message") == "from-subprocess" for e in received), (
            f"tailer did not republish event; received={received}"
        )
        # Origin tag was added
        msg_evt = next(e for e in received if e.get("message") == "from-subprocess")
        assert msg_evt["context"]["origin"] == "manual_execute"

    def test_init_tail_pos_skips_historical(self, tmp_path):
        """On boot, the bridge must NOT replay events that existed before startup."""
        from systemu.interface.manual_event_bridge import ManualEventBridge
        from systemu.interface.event_bus import EventBus
        import json, time

        EventBus._instance = None
        ManualEventBridge._instance = None

        # Pre-create the bridge file with a historical event
        bridge_file = tmp_path / "manual_events.jsonl"
        bridge_file.write_text(json.dumps({"category": "shadow", "message": "OLD"}) + "\n",
                               encoding="utf-8")

        bridge = ManualEventBridge.start(str(tmp_path))
        received: list = []
        EventBus.get().subscribe(lambda e: received.append(e), replay=False)

        time.sleep(2)   # tailer would have processed any new lines by now
        bridge._stop_event.set()

        assert not any(e.get("message") == "OLD" for e in received), (
            "historical event was replayed — _init_tail_pos broken"
        )


class TestCliBridgeInstall:
    def test_army_execute_calls_install_bridge_when_env_set(self, monkeypatch):
        """Verify the army_execute CLI command initializes the bridge writer
        when SYSTEMU_EVENT_BRIDGE_FILE env var is set."""
        from systemu.interface import cli_commands as cli

        spy = MagicMock()
        monkeypatch.setattr(
            "systemu.interface.event_bridge_writer.install_bridge_writer",
            spy,
        )
        monkeypatch.setenv("SYSTEMU_EVENT_BRIDGE_FILE", "/tmp/bridge.jsonl")
        cli._maybe_install_bridge_writer()
        spy.assert_called_once_with("/tmp/bridge.jsonl")

    def test_army_execute_skips_install_when_env_unset(self, monkeypatch):
        from systemu.interface import cli_commands as cli

        spy = MagicMock()
        monkeypatch.setattr(
            "systemu.interface.event_bridge_writer.install_bridge_writer",
            spy,
        )
        monkeypatch.delenv("SYSTEMU_EVENT_BRIDGE_FILE", raising=False)
        cli._maybe_install_bridge_writer()
        spy.assert_not_called()


# ─── Subsystem 6: Army UI view models ──────────────────────────────────────

class TestArmyJobsPanelViewModel:
    def test_view_model_groups_by_status(self):
        from systemu.interface.pages.army import _build_execute_jobs_panel_view_model
        from systemu.interface.jobs import Job, JobStatus
        from datetime import datetime

        class _Stub:
            def __init__(self):
                self.jobs = {
                    "q1": Job(id="q1", name="q1", type="execute",
                              status=JobStatus.QUEUED, start_time=datetime.now()),
                    "r1": Job(id="r1", name="r1", type="execute",
                              status=JobStatus.RUNNING, start_time=datetime.now()),
                    "c1": Job(id="c1", name="c1", type="execute",
                              status=JobStatus.COMPLETED, start_time=datetime.now()),
                    "non": Job(id="non", name="non", type="capture",
                               status=JobStatus.RUNNING, start_time=datetime.now()),
                }

        vm = _build_execute_jobs_panel_view_model(_Stub())
        assert len(vm["queued"]) == 1
        assert len(vm["running"]) == 1
        assert len(vm["completed"]) == 1
        # capture job filtered out
        for group in ("queued", "running", "completed"):
            assert all(j["type"] == "execute" for j in vm[group])

    def test_view_model_limits_total_to_30(self):
        from systemu.interface.pages.army import _build_execute_jobs_panel_view_model
        from systemu.interface.jobs import Job, JobStatus
        from datetime import datetime

        class _Stub:
            def __init__(self):
                self.jobs = {
                    f"c{i}": Job(id=f"c{i}", name=f"c{i}", type="execute",
                                 status=JobStatus.COMPLETED, start_time=datetime.now())
                    for i in range(50)
                }
        vm = _build_execute_jobs_panel_view_model(_Stub())
        # 30 total cap across all three columns (per spec)
        total = len(vm["queued"]) + len(vm["running"]) + len(vm["completed"])
        assert total <= 30


class TestScheduleDialogValidation:
    def test_validate_once_payload_in_past_rejected(self):
        from systemu.interface.pages.army import _validate_schedule_payload
        from datetime import datetime, timedelta, timezone

        past = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
        ok, err = _validate_schedule_payload(
            mode="once", scheduled_at=past, interval_minutes=None, end_at=None,
        )
        assert ok is False
        assert "past" in err.lower()

    def test_validate_recurring_interval_too_small(self):
        from systemu.interface.pages.army import _validate_schedule_payload
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)).isoformat()
        ok, err = _validate_schedule_payload(
            mode="recurring", scheduled_at=future, interval_minutes=2, end_at=None,
        )
        assert ok is False
        assert "5" in err

    def test_validate_recurring_ok(self):
        from systemu.interface.pages.army import _validate_schedule_payload
        from datetime import datetime, timedelta, timezone

        future = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)).isoformat()
        ok, err = _validate_schedule_payload(
            mode="recurring", scheduled_at=future, interval_minutes=30, end_at=None,
        )
        assert ok is True
        assert err == ""


class TestSchedulesListViewModel:
    def test_schedules_list_renders_relative_time(self, tmp_path):
        from systemu.interface.pages.army import _build_schedules_list_view_model
        from systemu.scheduler.schedule_registry import create_schedule
        from systemu.core.models import ScheduleMode
        from datetime import datetime, timedelta, timezone

        v = MagicMock()
        v.root = str(tmp_path)
        v.list_shadows = MagicMock(return_value=[{"id": "shadow_x", "name": "Tester"}])
        v.load_index = MagicMock(side_effect=lambda kind:
            [{"id": "scroll_y", "name": "TestScroll"}] if kind == "scrolls" else []
        )

        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=45)
        create_schedule(
            shadow_id="shadow_x", scroll_id="scroll_y",
            mode=ScheduleMode.ONCE, scheduled_at=future, vault=v,
        )

        vm = _build_schedules_list_view_model(v)
        assert len(vm) == 1
        e = vm[0]
        assert e["shadow_name"] == "Tester"
        assert e["scroll_name"] == "TestScroll"
        assert "min" in e["next_fire_relative"].lower() or "h" in e["next_fire_relative"].lower()

    def test_schedules_list_empty(self, tmp_path):
        from systemu.interface.pages.army import _build_schedules_list_view_model
        v = MagicMock()
        v.root = str(tmp_path)
        v.list_shadows = MagicMock(return_value=[])
        v.load_index = MagicMock(return_value=[])
        vm = _build_schedules_list_view_model(v)
        assert vm == []


class TestEventsFilter:
    def test_filter_manual_returns_only_manual_events(self):
        from systemu.interface.pages.notifications_page import _filter_events

        all_events = [
            {"category": "shadow", "message": "auto1", "context": {}},
            {"category": "shadow", "message": "manual1",
             "context": {"origin": "manual_execute"}},
            {"category": "supervisor", "message": "auto2", "context": {}},
            {"category": "shadow", "message": "manual2",
             "context": {"origin": "manual_execute"}},
        ]
        out = _filter_events(all_events, "manual")
        assert [e["message"] for e in out] == ["manual1", "manual2"]

    def test_filter_system_returns_non_manual(self):
        from systemu.interface.pages.notifications_page import _filter_events
        all_events = [
            {"category": "shadow", "message": "a", "context": {}},
            {"category": "shadow", "message": "b",
             "context": {"origin": "manual_execute"}},
        ]
        out = _filter_events(all_events, "system")
        assert [e["message"] for e in out] == ["a"]

    def test_filter_all_returns_everything(self):
        from systemu.interface.pages.notifications_page import _filter_events
        all_events = [
            {"category": "shadow", "message": "a", "context": {}},
            {"category": "shadow", "message": "b",
             "context": {"origin": "manual_execute"}},
        ]
        out = _filter_events(all_events, "all")
        assert len(out) == 2
