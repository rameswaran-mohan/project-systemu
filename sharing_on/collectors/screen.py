"""Screen capture collector — periodic screenshots using mss (cross-platform)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore

logger = logging.getLogger(__name__)


class ScreenCollector(BaseCollector):
    """Captures periodic screenshots of the primary monitor.

    Uses `mss` for fast, cross-platform screen capture.
    Screenshots are saved as compressed PNGs to the output directory.
    """

    name = "screen"

    def __init__(
        self,
        event_store: EventStore,
        output_dir: Path,
        interval: float = 3.0,
        max_width: int = 1280,
    ):
        super().__init__(event_store)
        self._interval = interval
        self._max_width = max_width
        self._screenshot_dir = output_dir / "screenshots"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._capture_count = 0

    def _collect_loop(self) -> None:
        import mss
        from PIL import Image

        with mss.mss() as sct:
            # Use primary monitor
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

            while self._running:
                try:
                    # Capture
                    raw = sct.grab(monitor)

                    # Convert to PIL for resizing and compression
                    img = Image.frombytes("RGB", raw.size, raw.rgb)

                    # Downscale if wider than max_width
                    if img.width > self._max_width:
                        ratio = self._max_width / img.width
                        new_size = (self._max_width, int(img.height * ratio))
                        img = img.resize(new_size, Image.LANCZOS)

                    # Save with timestamp-based filename
                    ts = datetime.now(timezone.utc)
                    filename = f"screen_{ts.strftime('%H%M%S')}_{self._capture_count:04d}.png"
                    filepath = self._screenshot_dir / filename
                    img.save(filepath, "PNG", optimize=True)

                    # Emit event
                    self.emit(CaptureEvent(
                        category=EventCategory.SCREEN,
                        action=EventAction.SCREENSHOT,
                        timestamp=ts,
                        file_path=str(filepath),
                        data={
                            "width": img.width,
                            "height": img.height,
                            "filename": filename,
                        },
                    ))

                    self._capture_count += 1

                except Exception as e:
                    logger.warning(f"Screenshot failed: {e}")

                # Wait for next capture
                time.sleep(self._interval)

        logger.info(f"Screen collector: captured {self._capture_count} screenshots")
