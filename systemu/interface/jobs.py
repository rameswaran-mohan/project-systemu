import os
import sys
import time
import uuid
import signal
import logging
import subprocess
import threading
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


class JobStatus(Enum):
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


class JobManager:
    _instance = None

    def __init__(self):
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.Lock()

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
    ) -> Job:
        job_id = str(uuid.uuid4())[:8]

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
        )
        with self.lock:
            self.jobs[job_id] = job

        # Monitor in background — pass log_fp so the thread can close it on exit
        threading.Thread(
            target=self._poll, args=(job, log_fp), daemon=True
        ).start()
        return job

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
