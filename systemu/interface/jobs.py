import os
import sys
import time
import uuid
import signal
import logging
import subprocess
import threading
from collections import deque
from pathlib import Path
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict

logger = logging.getLogger(__name__)

# Hard time-limit per job.  Jobs that exceed this are force-killed and
# marked FAILED so they never linger as phantom "active" tasks.
_JOB_TIMEOUT_SECONDS = 7200   # 2 hours
_POLL_INTERVAL       = 1.0    # seconds between process.poll() checks
_TERMINAL_KEEP       = 20     # max completed/failed/cancelled jobs retained in dict

# v0.8.6: bounded concurrency for execute jobs (operator-initiated subprocess flows)
JOBMANAGER_MAX_CONCURRENT_EXECUTE = int(
    os.environ.get("JOBMANAGER_MAX_CONCURRENT_EXECUTE", "3")
)
JOBMANAGER_MAX_EXECUTE_QUEUE_DEPTH = int(
    os.environ.get("JOBMANAGER_MAX_EXECUTE_QUEUE_DEPTH", "50")
)


class JobStatus(Enum):
    QUEUED    = "queued"      # v0.8.6: waiting for an execute slot
    RUNNING   = "running"
    STOPPING  = "stopping"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED    = "failed"


@dataclass
class Job:
    id:         str
    name:       str                                     # e.g. "Forge Tool: browser_click"
    type:       str                                     # e.g. "forge", "capture", "execute"
    status:     JobStatus
    process:    Optional[subprocess.Popen] = None
    output_dir: Optional[str]             = None
    on_cancel:  Optional[Callable[['Job'], None]] = None
    start_time: datetime = field(default_factory=datetime.now)  # per-instance, not shared
    dedup_key:  str = ""    # v0.8.6: scope-of-uniqueness for execute jobs


class JobManager:
    _instance = None

    def __init__(self):
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.Lock()
        self._execute_pending: deque = deque()   # v0.8.6: queued execute jobs
        # v0.8.6: background dispatcher drains QUEUED execute jobs when slots free.
        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher_loop,
            daemon=True,
            name="jobs-execute-dispatcher",
        )
        self._dispatcher_thread.start()

    @classmethod
    def get(cls) -> 'JobManager':
        if cls._instance is None:
            cls._instance = JobManager()
        return cls._instance

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_active_jobs(self) -> list[Job]:
        """Return jobs that are currently RUNNING or STOPPING."""
        with self.lock:
            return [j for j in self.jobs.values()
                    if j.status in (JobStatus.RUNNING, JobStatus.STOPPING)]

    def has_active_capture(self) -> bool:
        with self.lock:
            return any(
                j.type == "capture" and j.status == JobStatus.RUNNING
                for j in self.jobs.values()
            )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start_job(
        self,
        name:       str,
        job_type:   str,
        cmd:        list,
        cwd:        str,
        on_cancel:  Callable = None,
        output_dir: str = None,
        dedup_key:  str = "",
    ) -> Job:
        # v0.8.7: dedup_key is stored on the Job for display + traceability
        # but does NOT suppress spawn. The operator's intent ("I clicked Execute
        # again") is honored. UI can use find_active_by_dedup_key() to surface
        # an informational notice ("N other runs for this shadow+scroll are
        # in flight"), but the spawn proceeds either way. Queue + cancel give
        # the operator the safety net.
        job_id = str(uuid.uuid4())[:8]

        # v0.8.6: for execute jobs, check concurrency cap BEFORE spawning.
        if job_type == "execute":
            with self.lock:
                running_execute_count = sum(
                    1 for j in self.jobs.values()
                    if j.type == "execute" and j.status == JobStatus.RUNNING
                )
                queued_execute_count = sum(
                    1 for j in self.jobs.values()
                    if j.type == "execute" and j.status == JobStatus.QUEUED
                )
                total_execute = running_execute_count + queued_execute_count
                if total_execute >= JOBMANAGER_MAX_EXECUTE_QUEUE_DEPTH:
                    raise RuntimeError(
                        f"execute queue full ({total_execute} jobs in flight; "
                        f"cap is {JOBMANAGER_MAX_EXECUTE_QUEUE_DEPTH})"
                    )
                slot_free = running_execute_count < JOBMANAGER_MAX_CONCURRENT_EXECUTE

            if not slot_free:
                # Enqueue: create a Job with status=QUEUED, no Popen
                job = Job(
                    id=job_id, name=name, type=job_type,
                    status=JobStatus.QUEUED, process=None,
                    output_dir=output_dir, on_cancel=on_cancel,
                    dedup_key=dedup_key,
                )
                # Stash cmd+cwd as private attrs so the dispatcher can spawn later
                job._cmd = list(cmd)
                job._cwd = cwd
                with self.lock:
                    self.jobs[job_id] = job
                    self._execute_pending.append(job)
                logger.info(
                    "[Jobs] Execute job %s enqueued (position %d) — running=%d/%d",
                    job_id, len(self._execute_pending),
                    running_execute_count, JOBMANAGER_MAX_CONCURRENT_EXECUTE,
                )
                return job

        # Spawn immediately (non-execute, or execute with free slot)
        return self._spawn_job(job_id, name, job_type, cmd, cwd, on_cancel, output_dir, dedup_key)

    def _spawn_job(self, job_id, name, job_type, cmd, cwd, on_cancel, output_dir, dedup_key):
        """Inner helper: build child env, Popen, register, start poll thread."""
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"]       = "1"
        # Subprocesses spawned by the dashboard job manager are always headless —
        # stdout/stderr are redirected to a log file, no user at a terminal.
        # Setting this explicitly prevents notify_user() from blocking on input
        # even when the daemon itself runs in foreground/interactive mode.
        child_env["SYSTEMU_HEADLESS"] = "1"
        import systemu
        child_env["PYTHONPATH"] = str(Path(systemu.__file__).parent.parent.absolute())

        # New process group on Windows so CTRL_BREAK is scoped to the child only
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0

        # Pipe stdout+stderr to a shared log file
        vault_dir = os.environ.get("SYSTEMU_VAULT_DIR", str(Path(cwd) / "systemu" / "vault"))
        for candidate in [Path(cwd) / ".vault", Path(cwd) / "systemu" / "vault"]:
            if candidate.exists():
                vault_dir = str(candidate)
                break
        log_path = Path(vault_dir) / "jobs.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fp = open(log_path, "a", encoding="utf-8")

        # v0.8.6: execute jobs get a bridge file env so the subprocess can publish
        # events back to the dashboard's EventBus via manual_events.jsonl
        if job_type == "execute":
            child_env["SYSTEMU_EVENT_BRIDGE_FILE"] = str(Path(vault_dir) / "manual_events.jsonl")

        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=child_env,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

        job = Job(
            id=job_id,
            name=name,
            type=job_type,
            status=JobStatus.RUNNING,
            process=proc,
            output_dir=output_dir,
            on_cancel=on_cancel,
            dedup_key=dedup_key,
        )
        with self.lock:
            self.jobs[job_id] = job

        # Monitor in background — pass log_fp so the thread can close it on exit
        threading.Thread(
            target=self._poll, args=(job, log_fp), daemon=True
        ).start()
        return job

    def find_active_by_dedup_key(self, dedup_key: str) -> Optional[Job]:
        """Return the first non-terminal job matching dedup_key, or None."""
        if not dedup_key:
            return None
        with self.lock:
            for job in self.jobs.values():
                if (job.dedup_key == dedup_key and
                    job.status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.STOPPING)):
                    return job
        return None

    def stop_job_gracefully(self, job_id: str) -> None:
        """Send SIGINT / CTRL_BREAK to let the process flush and clean up."""
        with self.lock:
            if job_id not in self.jobs:
                return
            job = self.jobs[job_id]
            if job.status != JobStatus.RUNNING:
                return
            job.status = JobStatus.STOPPING   # instant UI feedback

        try:
            if sys.platform == "win32":
                os.kill(job.process.pid, signal.CTRL_BREAK_EVENT)
            else:
                job.process.send_signal(signal.SIGINT)
        except Exception:
            job.process.terminate()

    def cancel_job_hard(self, job_id: str) -> None:
        """Hard-kill the process and invoke the rollback handler."""
        with self.lock:
            if job_id not in self.jobs:
                return
            job = self.jobs[job_id]
            if job.status not in (JobStatus.RUNNING, JobStatus.STOPPING):
                return
            try:
                job.process.kill()
            except Exception:
                pass
            job.status = JobStatus.CANCELLED

        # Run rollback outside the lock to prevent deadlock if the callback is slow
        if job.on_cancel:
            try:
                job.on_cancel(job)
            except Exception as exc:
                logger.error("[Jobs] Rollback handler for job %s raised: %s", job_id, exc)

    def cancel_queued(self, job_id: str) -> bool:
        """Cancel a QUEUED execute job: remove from pending deque + mark CANCELLED.

        Does NOT affect RUNNING jobs (use cancel_job_hard for those).
        Returns True if cancelled, False if not found or not QUEUED.
        """
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return False
            try:
                self._execute_pending.remove(job)
            except ValueError:
                pass   # already popped by dispatcher between status check + here
            job.status = JobStatus.CANCELLED
            logger.info("[Jobs] Queued execute job %s cancelled by operator", job_id)
            return True

    def _dispatcher_tick(self) -> None:
        """One iteration of the execute dispatcher: promote oldest QUEUED if slot free."""
        job_to_spawn: Optional[Job] = None
        with self.lock:
            running_execute_count = sum(
                1 for j in self.jobs.values()
                if j.type == "execute" and j.status == JobStatus.RUNNING
            )
            if running_execute_count >= JOBMANAGER_MAX_CONCURRENT_EXECUTE:
                return
            # Pop oldest QUEUED, skipping any that were cancelled between
            # enqueue and now.
            while self._execute_pending:
                candidate = self._execute_pending.popleft()
                if candidate.status == JobStatus.QUEUED:
                    job_to_spawn = candidate
                    break

        if job_to_spawn is None:
            return

        # Reconstruct the cmd from job metadata; cmd+cwd were stashed on the
        # Job during enqueue as private attrs. _spawn_job will flip status to RUNNING.
        try:
            self._spawn_job(
                job_to_spawn.id, job_to_spawn.name, job_to_spawn.type,
                getattr(job_to_spawn, "_cmd", []),
                getattr(job_to_spawn, "_cwd", ""),
                job_to_spawn.on_cancel,
                job_to_spawn.output_dir,
                job_to_spawn.dedup_key,
            )
            logger.info("[Jobs] Dispatcher promoted queued job %s to RUNNING", job_to_spawn.id)
        except Exception:
            logger.exception("[Jobs] Dispatcher failed to spawn queued job %s", job_to_spawn.id)
            with self.lock:
                job_to_spawn.status = JobStatus.FAILED

    def _dispatcher_loop(self) -> None:
        """Background thread: drain QUEUED execute jobs into RUNNING as slots free."""
        while True:
            try:
                self._dispatcher_tick()
            except Exception:
                logger.exception("[Jobs] Dispatcher loop error — continuing")
            time.sleep(1.0)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _poll(self, job: Job, log_fp=None) -> None:
        """Non-blocking poll loop with 2-hour watchdog. Closes log_fp on exit."""
        deadline = time.monotonic() + _JOB_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                ret = job.process.poll()
                if ret is not None:
                    # Process has exited
                    with self.lock:
                        if job.status in (JobStatus.RUNNING, JobStatus.STOPPING):
                            job.status = (
                                JobStatus.COMPLETED if ret == 0 else JobStatus.FAILED
                            )
                    return

                # Respect external hard-kill (cancel_job_hard sets CANCELLED first)
                with self.lock:
                    if job.status == JobStatus.CANCELLED:
                        return

                time.sleep(_POLL_INTERVAL)

            # ── Watchdog: process exceeded time limit ─────────────────────────
            logger.warning(
                "[Jobs] Job '%s' (%s) exceeded %ds — force-killing",
                job.name, job.id, _JOB_TIMEOUT_SECONDS,
            )
            with self.lock:
                if job.status in (JobStatus.RUNNING, JobStatus.STOPPING):
                    try:
                        job.process.kill()
                    except Exception:
                        pass
                    job.status = JobStatus.FAILED

        finally:
            # Always close the log file handle regardless of how we exit
            if log_fp is not None:
                try:
                    log_fp.flush()
                    log_fp.close()
                except Exception:
                    pass
            # Prune old terminal jobs to keep the dict bounded
            with self.lock:
                self._prune_old_jobs()

    def _prune_old_jobs(self) -> None:
        """Drop the oldest terminal jobs beyond _TERMINAL_KEEP. Caller holds lock."""
        terminal = [
            jid for jid, j in self.jobs.items()
            if j.status not in (JobStatus.RUNNING, JobStatus.STOPPING)
        ]
        if len(terminal) <= _TERMINAL_KEEP:
            return
        # Sort oldest-first and remove the excess
        oldest = sorted(terminal, key=lambda jid: self.jobs[jid].start_time)
        for jid in oldest[:len(oldest) - _TERMINAL_KEEP]:
            del self.jobs[jid]
