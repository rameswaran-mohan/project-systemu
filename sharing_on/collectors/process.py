"""Process monitor collector — tracks process starts and stops.

Uses `psutil` for cross-platform process enumeration.
Polls periodically and detects new / terminated processes.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Set

import psutil

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore

logger = logging.getLogger(__name__)

# Processes to ignore (OS internals, system services)
IGNORED_PROCESSES = {
    "svchost.exe", "csrss.exe", "smss.exe", "lsass.exe", "services.exe",
    "wininit.exe", "winlogon.exe", "dwm.exe", "fontdrvhost.exe",
    "conhost.exe", "RuntimeBroker.exe", "SearchHost.exe",
    "ShellExperienceHost.exe", "StartMenuExperienceHost.exe",
    "TextInputHost.exe", "SecurityHealthSystray.exe", "ctfmon.exe",
    "System", "System Idle Process", "Registry",
    # Linux system processes
    "systemd", "kthreadd", "ksoftirqd", "kworker", "rcu_sched",
    "migration", "watchdog",
}


class ProcessCollector(BaseCollector):
    """Detects new and terminated processes during the capture session.

    Uses polling (via psutil) rather than kernel hooks — simpler,
    cross-platform, and sufficient for step detection.
    """

    name = "process"

    def __init__(
        self,
        event_store: EventStore,
        poll_interval: float = 2.0,
    ):
        super().__init__(event_store)
        self._poll_interval = poll_interval
        self._known_pids: Dict[int, str] = {}  # pid -> process_name

    def _collect_loop(self) -> None:
        # Initial snapshot — record all currently running processes
        self._known_pids = self._get_current_processes()

        while self._running:
            current = self._get_current_processes()

            # Detect new processes
            new_pids = set(current.keys()) - set(self._known_pids.keys())
            for pid in new_pids:
                name = current[pid]
                if name in IGNORED_PROCESSES:
                    continue
                try:
                    proc = psutil.Process(pid)
                    cmdline = " ".join(proc.cmdline()) if proc.cmdline() else name
                    # Truncate very long command lines
                    if len(cmdline) > 500:
                        cmdline = cmdline[:500] + "..."

                    self.emit(CaptureEvent(
                        category=EventCategory.PROCESS,
                        action=EventAction.PROCESS_STARTED,
                        timestamp=datetime.now(timezone.utc),
                        process_name=name,
                        data={
                            "pid": pid,
                            "cmdline": cmdline,
                            "exe": proc.exe() if self._safe_exe(proc) else "",
                        },
                    ))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            # Detect terminated processes
            gone_pids = set(self._known_pids.keys()) - set(current.keys())
            for pid in gone_pids:
                name = self._known_pids[pid]
                if name in IGNORED_PROCESSES:
                    continue
                self.emit(CaptureEvent(
                    category=EventCategory.PROCESS,
                    action=EventAction.PROCESS_ENDED,
                    timestamp=datetime.now(timezone.utc),
                    process_name=name,
                    data={"pid": pid},
                ))

            self._known_pids = current
            time.sleep(self._poll_interval)

    @staticmethod
    def _get_current_processes() -> Dict[int, str]:
        """Get a snapshot of all running processes as {pid: name}."""
        processes = {}
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                processes[proc.info["pid"]] = proc.info["name"] or "Unknown"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return processes

    @staticmethod
    def _safe_exe(proc: psutil.Process) -> bool:
        """Check if we can safely access proc.exe()."""
        try:
            proc.exe()
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False
