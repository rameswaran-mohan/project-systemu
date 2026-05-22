"""Step boundary detector — groups raw events into logical steps.

Uses heuristics to identify when the user moved from one logical step
to another during their task. The output is a list of Steps, each
containing the events that belong to that step and a representative
screenshot.

Step boundaries are detected by:
1. User-placed markers (highest priority)
2. Application/window switches
3. Time gaps (> idle_threshold seconds between events)
4. File save events following a sequence of modifications
5. Terminal command executions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sharing_on.events.models import (
    CaptureEvent,
    EventAction,
    EventCategory,
)

logger = logging.getLogger(__name__)


@dataclass
class Step:
    """A logical step in the user's task — a group of related events."""

    step_number: int
    events: List[CaptureEvent] = field(default_factory=list)
    label: Optional[str] = None             # from user marker, or auto-generated
    screenshot_path: Optional[str] = None   # best screenshot for this step
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    primary_app: Optional[str] = None       # most-used app during this step

    @property
    def duration_seconds(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0

    @property
    def event_summary(self) -> Dict[str, int]:
        """Count events by category."""
        counts: Dict[str, int] = {}
        for e in self.events:
            key = e.category.value
            counts[key] = counts.get(key, 0) + 1
        return counts

    def get_file_events(self) -> List[CaptureEvent]:
        """Get all file-related events in this step."""
        return [
            e for e in self.events
            if e.category == EventCategory.FILE
        ]

    def get_process_events(self) -> List[CaptureEvent]:
        """Get process start/stop events in this step."""
        return [
            e for e in self.events
            if e.category == EventCategory.PROCESS
        ]

    def get_window_events(self) -> List[CaptureEvent]:
        """Get window focus events in this step."""
        return [
            e for e in self.events
            if e.category == EventCategory.WINDOW
        ]

    def get_clipboard_events(self) -> List[CaptureEvent]:
        """Get clipboard events in this step."""
        return [
            e for e in self.events
            if e.category == EventCategory.CLIPBOARD
        ]


class StepDetector:
    """Groups raw captured events into logical task steps."""

    def __init__(self, idle_threshold: float = 10.0):
        self._idle_threshold = timedelta(seconds=idle_threshold)

    def detect_steps(self, events: List[CaptureEvent]) -> List[Step]:
        """Analyze a list of events and group them into steps.

        Args:
            events: All captured events, ordered by timestamp.

        Returns:
            A list of Steps, each containing its constituent events.
        """
        if not events:
            return []

        # Filter out session lifecycle and screenshot-only events for boundary detection
        # (screenshots are assigned to steps later)
        meaningful_events = [
            e for e in events
            if e.category != EventCategory.SESSION
        ]

        if not meaningful_events:
            return []

        # Pass 1: Find boundary indices
        boundaries = self._find_boundaries(meaningful_events)

        # Pass 2: Split events into steps at boundary points
        raw_steps = self._split_at_boundaries(meaningful_events, boundaries)

        # Pass 3: Assign screenshots and metadata to each step
        steps = self._enrich_steps(raw_steps)

        # Pass 4: Merge tiny steps (< 2 meaningful events) into adjacent steps
        steps = self._merge_tiny_steps(steps)

        logger.info(f"Detected {len(steps)} steps from {len(events)} events")
        return steps

    def _find_boundaries(self, events: List[CaptureEvent]) -> List[int]:
        """Find indices where step boundaries occur."""
        boundaries: List[int] = []

        for i in range(1, len(events)):
            prev = events[i - 1]
            curr = events[i]

            # Boundary: user-placed marker
            if curr.action == EventAction.STEP_MARKER:
                boundaries.append(i)
                continue

            # Boundary: application switch (different app, not just title change)
            if (
                curr.category == EventCategory.WINDOW
                and curr.action == EventAction.WINDOW_FOCUS
                and prev.application
                and curr.application
                and curr.application != prev.application
                # Ignore switches to/from the same app
                and curr.application != self._last_app_before(events, i)
            ):
                boundaries.append(i)
                continue

            # Boundary: application switch detected via interaction events
            if (
                curr.category == EventCategory.INTERACTION
                and prev.application
                and curr.application
                and curr.application != prev.application
                and curr.application not in ("Unknown", "Chrome/Edge Web Browser")
            ):
                boundaries.append(i)
                continue

            # Boundary: significant time gap
            time_gap = curr.timestamp - prev.timestamp
            if time_gap > self._idle_threshold:
                # Ignore gaps between consecutive screenshots
                if not (
                    prev.category == EventCategory.SCREEN
                    and curr.category == EventCategory.SCREEN
                ):
                    boundaries.append(i)
                    continue

            # Boundary: terminal command execution (process start with shell-like parent)
            if (
                curr.action == EventAction.PROCESS_STARTED
                and curr.data.get("cmdline")
                and self._looks_like_user_command(curr)
            ):
                boundaries.append(i)
                continue

        return sorted(set(boundaries))

    def _split_at_boundaries(
        self, events: List[CaptureEvent], boundaries: List[int]
    ) -> List[List[CaptureEvent]]:
        """Split the event list into groups at boundary indices."""
        if not boundaries:
            return [events]

        groups: List[List[CaptureEvent]] = []
        prev_idx = 0
        for boundary_idx in boundaries:
            group = events[prev_idx:boundary_idx]
            if group:
                groups.append(group)
            prev_idx = boundary_idx

        # Last group
        remaining = events[prev_idx:]
        if remaining:
            groups.append(remaining)

        return groups

    def _enrich_steps(self, raw_steps: List[List[CaptureEvent]]) -> List[Step]:
        """Convert raw event groups into enriched Step objects."""
        steps = []
        for i, event_group in enumerate(raw_steps):
            if not event_group:
                continue

            step = Step(
                step_number=i + 1,
                events=event_group,
                start_time=event_group[0].timestamp,
                end_time=event_group[-1].timestamp,
            )

            # Assign label from marker if present
            markers = [
                e for e in event_group
                if e.action == EventAction.STEP_MARKER
            ]
            if markers:
                step.label = markers[0].data.get("label", f"Step {i + 1}")

            # Find the primary application used in this step
            app_counts: Dict[str, int] = {}
            for e in event_group:
                if e.application:
                    app_counts[e.application] = app_counts.get(e.application, 0) + 1
            if app_counts:
                step.primary_app = max(app_counts, key=app_counts.get)  # type: ignore

            # Assign the best screenshot (closest to the middle of the step)
            screenshots = [
                e for e in event_group
                if e.action == EventAction.SCREENSHOT and e.file_path
            ]
            if screenshots:
                # Pick the screenshot closest to the midpoint
                if step.start_time and step.end_time:
                    midpoint = step.start_time + (step.end_time - step.start_time) / 2
                    best = min(
                        screenshots,
                        key=lambda s: abs((s.timestamp - midpoint).total_seconds()),
                    )
                else:
                    best = screenshots[len(screenshots) // 2]
                step.screenshot_path = best.file_path

            steps.append(step)

        return steps

    def _merge_tiny_steps(
        self, steps: List[Step], min_meaningful: int = 2
    ) -> List[Step]:
        """Merge steps with very few meaningful events into adjacent steps."""
        if len(steps) <= 1:
            return steps

        merged: List[Step] = [steps[0]]
        for step in steps[1:]:
            # Count non-screenshot events
            meaningful = [
                e for e in step.events
                if e.category != EventCategory.SCREEN
            ]

            if len(meaningful) < min_meaningful and merged:
                # Merge into the previous step
                prev = merged[-1]
                prev.events.extend(step.events)
                prev.end_time = step.end_time
                # Keep the screenshot from the merged step if previous has none
                if step.screenshot_path and not prev.screenshot_path:
                    prev.screenshot_path = step.screenshot_path
            else:
                merged.append(step)

        # Renumber
        for i, step in enumerate(merged):
            step.step_number = i + 1

        return merged

    @staticmethod
    def _last_app_before(events: List[CaptureEvent], idx: int) -> Optional[str]:
        """Find the app name from the most recent window event before idx."""
        for i in range(idx - 1, -1, -1):
            if events[i].application and events[i].category == EventCategory.WINDOW:
                return events[i].application
        return None

    @staticmethod
    def _looks_like_user_command(event: CaptureEvent) -> bool:
        """Heuristic: does this process start look like a user-typed command?"""
        cmdline = event.data.get("cmdline", "")
        name = event.process_name or ""

        # Known shell commands / development tools
        user_tools = {
            "git", "npm", "node", "python", "pip", "docker", "kubectl",
            "curl", "wget", "ssh", "scp", "rsync", "make", "cargo",
            "go", "dotnet", "mvn", "gradle", "terraform", "ansible",
            "powershell", "pwsh", "bash", "sh", "zsh",
        }

        name_lower = name.lower().replace(".exe", "")
        return name_lower in user_tools
