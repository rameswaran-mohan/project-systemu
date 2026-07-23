"""Dashboard-side event bridge tailer (v0.8.6).

Tails vault/manual_events.jsonl in a background daemon thread. Each new line
is parsed as a JSON event, tagged with context.origin='manual_execute', and
republished onto the dashboard's in-process EventBus singleton.

This lets execute-subprocess events (which live in their own subprocess's
EventBus instance) surface in the dashboard's Live Events panel even though
they cross a process boundary.

Rotation: when the bridge file exceeds 10 MB, it's renamed to .jsonl.1
(one-deep); the tailer transparently picks up the new empty file on the
next iteration.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ROTATION_THRESHOLD_BYTES = 10 * 1024 * 1024
_TAIL_INTERVAL_S = 1.0


class ManualEventBridge:
    """Singleton background tailer; one per dashboard process."""

    _instance: Optional["ManualEventBridge"] = None

    def __init__(self, bridge_file_path: str):
        self._path = bridge_file_path
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tail_pos: int = 0

    @classmethod
    def start(cls, vault_dir: str) -> "ManualEventBridge":
        """Idempotent — safe to call multiple times during dashboard boot."""
        if cls._instance is None:
            path = os.path.join(vault_dir, "manual_events.jsonl")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).touch(exist_ok=True)
            cls._instance = cls(path)
            cls._instance._init_tail_pos()
            cls._instance._thread = threading.Thread(
                target=cls._instance._tail_loop,
                daemon=True,
                name="manual-event-bridge",
            )
            cls._instance._thread.start()
            logger.info("[ManualBridge] started tailing %s", path)
        return cls._instance

    def _init_tail_pos(self) -> None:
        """Seek to end on boot so historical events aren't replayed."""
        try:
            self._tail_pos = os.path.getsize(self._path)
        except Exception:
            self._tail_pos = 0

    def _tail_loop(self) -> None:
        from systemu.interface.event_bus import EventBus
        bus = EventBus.get()

        while not self._stop_event.is_set():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    f.seek(self._tail_pos, 0)
                    while not self._stop_event.is_set():
                        line = f.readline()
                        if not line:
                            self._tail_pos = f.tell()
                            break
                        try:
                            event = json.loads(line)
                            ctx = event.get("context", {})
                            ctx["origin"] = "manual_execute"
                            event["context"] = ctx
                            bus.publish(event)
                        except Exception:
                            logger.debug("[ManualBridge] malformed line — skipped")
                    self._tail_pos = f.tell()

                if self._check_rotation():
                    self._tail_pos = 0

                # offload-lint: ok — _tail_loop IS the bridge's own daemon
                # thread (started in start()); this interruptible wait is how it
                # paces itself, and it never runs on the event loop.
                if self._stop_event.wait(timeout=_TAIL_INTERVAL_S):
                    break
            except FileNotFoundError:
                Path(self._path).touch()
                self._tail_pos = 0
                # offload-lint: ok — same daemon thread as above
                if self._stop_event.wait(timeout=_TAIL_INTERVAL_S):
                    break
            except Exception:
                logger.exception("[ManualBridge] tail loop error — continuing")
                # offload-lint: ok — same daemon thread as above
                if self._stop_event.wait(timeout=_TAIL_INTERVAL_S):
                    break

    def _check_rotation(self) -> bool:
        """Rotate at 10 MB. Returns True if rotated."""
        try:
            if os.path.getsize(self._path) > _ROTATION_THRESHOLD_BYTES:
                backup = self._path + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(self._path, backup)
                Path(self._path).touch()
                logger.info("[ManualBridge] rotated %s -> .1", self._path)
                return True
        except Exception:
            logger.debug("[ManualBridge] rotation skipped")
        return False
