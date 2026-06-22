"""Session manager — orchestrates the full capture lifecycle.

Flow: start() → [user does their task] → stop() → analyze() → render()
"""

from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import queue

from sharing_on.collectors.base import BaseCollector
from sharing_on.collectors.input_hook import InputHookCollector
from sharing_on.collectors.introspectors import get_ui_introspector
from sharing_on.collectors.web_extension import WebExtensionCollector
from sharing_on.collectors.clipboard import ClipboardCollector
from sharing_on.collectors.filesystem import FileSystemCollector
from sharing_on.collectors.process import ProcessCollector
from sharing_on.collectors.screen import ScreenCollector
from sharing_on.collectors.window import WindowCollector
from sharing_on.collectors.sources import CaptureSources
from sharing_on.config import Config
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore
from sharing_on.platform_info import PlatformInfo, detect_platform

logger = logging.getLogger(__name__)


class CaptureSession:
    """Manages a single capture session from start to finish."""

    def __init__(
        self,
        name: str,
        config: Config,
        output_dir: Optional[Path] = None,
    ):
        self.name = name
        self.config = config
        self.platform = detect_platform()

        # Session metadata
        self.session_id = f"cap_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None

        # Output directory
        if output_dir:
            self.output_dir = output_dir
        else:
            safe_name = "".join(
                c if c.isalnum() or c in "-_ " else "_" for c in name
            ).strip().replace(" ", "_").lower()
            self.output_dir = (
                Path(config.output_base_dir or ".")
                / "captures"
                / f"{safe_name}_{self.session_id}"
            )
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Event store
        self._db_path = self.output_dir / "events.db"
        self._store = EventStore(self._db_path)

        # Collectors
        self._collectors: List[BaseCollector] = []
        self._running = False

    @property
    def event_count(self) -> int:
        return self._store.event_count

    @property
    def collector_status(self) -> List[dict]:
        """Get status of all collectors."""
        return [
            {
                "name": c.name,
                "running": c.is_running,
                "error": c.error,
            }
            for c in self._collectors
        ]

    def start(self) -> None:
        """Start all collectors and begin capturing."""
        self.start_time = datetime.now(timezone.utc)
        self._running = True

        # Initialize event store
        self._store.start()

        # Record session start event
        self._store.put(CaptureEvent(
            category=EventCategory.SESSION,
            action=EventAction.SESSION_START,
            timestamp=self.start_time,
            data={
                "session_id": self.session_id,
                "session_name": self.name,
                "platform": self.platform.summary(),
                "output_dir": str(self.output_dir),
            },
        ))

        # Build and start collectors
        self._build_collectors()
        for collector in self._collectors:
            try:
                collector.start()
            except Exception as e:
                logger.warning(f"Failed to start collector '{collector.name}': {e}")

        # Save session metadata
        self._save_metadata()

        logger.info(
            f"Capture session '{self.name}' started with "
            f"{len(self._collectors)} collectors"
        )

    def add_marker(self, label: str) -> None:
        """Add a user-placed step marker."""
        self._store.put(CaptureEvent(
            category=EventCategory.MARKER,
            action=EventAction.STEP_MARKER,
            timestamp=datetime.now(timezone.utc),
            data={"label": label},
        ))

    def stop(self) -> None:
        """Stop all collectors and seal the session."""
        self._running = False
        self.end_time = datetime.now(timezone.utc)

        # Stop all collectors
        for collector in reversed(self._collectors):
            try:
                collector.stop()
            except Exception as e:
                logger.warning(f"Error stopping collector '{collector.name}': {e}")

        # Record session stop event
        self._store.put(CaptureEvent(
            category=EventCategory.SESSION,
            action=EventAction.SESSION_STOP,
            timestamp=self.end_time,
            data={
                "session_id": self.session_id,
                "duration_seconds": (
                    self.end_time - self.start_time
                ).total_seconds() if self.start_time else 0,
                "event_count": self._store.event_count,
            },
        ))

        # Flush the event store
        self._store.stop()

        # Update metadata
        self._save_metadata()

        logger.info(
            f"Capture session '{self.name}' stopped. "
            f"{self._store.event_count} events captured."
        )

    def get_events(self) -> List[CaptureEvent]:
        """Get all captured events (for analysis)."""
        return self._store.get_all_events()

    # --- Internal ---

    def _build_sources(self) -> CaptureSources:
        """Build the capture-sources filter from config (all by default)."""
        return CaptureSources(
            mode=self.config.capture_sources_mode,
            source_app=self.config.capture_source_app,
            source_title=self.config.capture_source_title,
        )

    def _build_collectors(self) -> None:
        """Create collector instances based on platform capabilities."""
        caps = self.platform.capabilities

        # Screen capture (opt-in — images are never used by the LLM pipeline)
        if self.config.capture_screenshots and "screenshots" in caps:
            self._collectors.append(ScreenCollector(
                event_store=self._store,
                output_dir=self.output_dir,
                interval=self.config.screenshot_interval,
                max_width=self.config.screenshot_max_width,
            ))

        # Advanced Event-Driven Omni-Capture (Windows specific for now)
        # We instantiate a shared queue so the hook can safely hand off coordinates to the UI inspector
        self._coord_queue = queue.Queue()

        try:
            self._collectors.append(InputHookCollector(
                event_store=self._store,
                coord_queue=self._coord_queue,
            ))
        except ImportError as exc:
            logger.warning("InputHookCollector skipped (pynput not available): %s", exc)
        
        # UI Introspection (Cross-Platform OS Accessibility API)
        if "ui_introspection" in self.platform.capabilities:
            introspector = get_ui_introspector(
                os_type=self.platform.os_type,
                event_store=self._store,
                coordinate_queue=self._coord_queue
            )
            if introspector:
                self._collectors.append(introspector)
            
        # Web Extension Collector (Local HTTP server for Chrome Extension)
        self._collectors.append(WebExtensionCollector(
            event_store=self._store
        ))

        # Active window tracking (Fallback for Alt-Tab without clicking)
        if "window_tracker" in caps:
            self._collectors.append(WindowCollector(
                event_store=self._store,
                platform=self.platform,
                poll_interval=self.config.window_poll_interval,
            ))

        # File system watcher (only if watch dirs specified)
        if self.config.watch_dirs:
            self._collectors.append(FileSystemCollector(
                event_store=self._store,
                watch_dirs=self.config.watch_dirs,
                ignore_patterns=self.config.ignore_patterns,
            ))

        # Process monitor (always available)
        if "process_monitor" in caps:
            self._collectors.append(ProcessCollector(
                event_store=self._store,
                poll_interval=self.config.process_poll_interval,
            ))

        # Clipboard monitor
        if "clipboard" in caps:
            self._collectors.append(ClipboardCollector(
                event_store=self._store,
                platform=self.platform,
                poll_interval=self.config.clipboard_poll_interval,
            ))

        # v0.9.35 Phase 0: install the capture-sources filter on every collector
        # so emit() drops off-source events. "all" sources is a no-op.
        sources = self._build_sources()
        for collector in self._collectors:
            collector.set_sources(sources)

    def _save_metadata(self) -> None:
        """Save session metadata to a JSON file."""
        metadata = {
            "session_id": self.session_id,
            "name": self.name,
            "platform": self.platform.summary(),
            "os_type": self.platform.os_type.value,
            "capabilities": self.platform.capabilities,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "output_dir": str(self.output_dir),
            "event_count": self._store.event_count,
            "collectors": [c.name for c in self._collectors],
            "watch_dirs": self.config.watch_dirs,
            "capture_sources_mode": self.config.capture_sources_mode,
            "capture_source_app": self.config.capture_source_app,
            "capture_source_title": self.config.capture_source_title,
            # Back-compat: v0.9.34.1 session.json keys (broad/narrow tokens) so a
            # reader pinned to the old schema still parses these sessions.
            "capture_scope": (
                "narrow" if self.config.capture_sources_mode == "single" else "broad"
            ),
            "capture_target_app": self.config.capture_source_app,
            "capture_target_title": self.config.capture_source_title,
            # v0.9.35: record-time generalization toggle (broad|standard|narrow).
            "generalization": self.config.generalization_mode,
        }
        meta_path = self.output_dir / "session.json"
        meta_path.write_text(json.dumps(metadata, indent=2))
