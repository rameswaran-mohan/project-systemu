"""Subprocess-side event bridge writer (v0.8.6).

Subscribes to the local EventBus singleton; on every published event, appends
one JSON line to the bridge file. Designed for use inside an Execute
subprocess so its events can be picked up by the dashboard's tailer
(ManualEventBridge) and republished onto the dashboard's in-process bus.

Atomic-append-per-line: each write uses open(..., "a") which translates to
O_APPEND on POSIX and FILE_APPEND_DATA on Windows. The OS guarantees that
each write is contiguous, preventing interleaved partial lines across
concurrent writers.

Failure of the write is intentionally silent: subprocess execution must not
break because the dashboard's bridge file is unwritable.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


def install_bridge_writer(bridge_file_path: str) -> Callable[[], None]:
    """Subscribe to EventBus and mirror every event to bridge_file_path.

    Each mirrored event is stamped with ``SYSTEMU_STREAM_REF`` (the spawning
    job's id) so a dashboard rail can follow one specific run. An event that
    already carries its own ``stream_ref`` is left untouched.

    Returns the EventBus unsubscribe callable so callers/tests can detach.
    """
    from systemu.interface.event_bus import EventBus

    # Read the stream ref once at install time (set by JobManager._child_env).
    stream_ref = os.getenv("SYSTEMU_STREAM_REF", "")

    def _on_event(event: Dict[str, Any]) -> None:
        try:
            if stream_ref and not event.get("stream_ref"):
                event = {**event, "stream_ref": stream_ref}
            line = json.dumps(event, default=str) + "\n"
            with open(bridge_file_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            # Subprocess must continue even if bridge file is unwritable.
            pass

    unsubscribe = EventBus.get().subscribe(_on_event, replay=False)
    logger.debug("[EventBridgeWriter] installed for %s (stream_ref=%s)",
                 bridge_file_path, stream_ref or "—")
    return unsubscribe
