"""Canonical event models for all captured activity."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class EventCategory(Enum):
    """Top-level activity categories."""
    SCREEN = "screen"
    WINDOW = "window"
    FILE = "file"
    PROCESS = "process"
    CLIPBOARD = "clipboard"
    MARKER = "marker"         # user-placed step markers
    SESSION = "session"       # session lifecycle events
    INTERACTION = "interaction" # precise UI interactions (clicks/keys)


class EventAction(Enum):
    """Specific actions within each category."""
    # Screen
    SCREENSHOT = "screenshot"

    # Window
    WINDOW_FOCUS = "window_focus"
    WINDOW_TITLE_CHANGE = "window_title_change"

    # File
    FILE_CREATED = "file_created"
    FILE_MODIFIED = "file_modified"
    FILE_DELETED = "file_deleted"
    FILE_MOVED = "file_moved"

    # Process
    PROCESS_STARTED = "process_started"
    PROCESS_ENDED = "process_ended"

    # Clipboard
    CLIPBOARD_CHANGE = "clipboard_change"

    # Markers
    STEP_MARKER = "step_marker"

    # Session
    SESSION_START = "session_start"
    SESSION_STOP = "session_stop"

    # Interaction
    MOUSE_CLICK = "mouse_click"
    KEY_PRESS = "key_press"


@dataclass
class CaptureEvent:
    """A single captured activity event — the universal record format."""

    category: EventCategory
    action: EventAction
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    data: Dict[str, Any] = field(default_factory=dict)

    # Optional context
    application: Optional[str] = None      # e.g. "Visual Studio Code"
    window_title: Optional[str] = None     # e.g. "main.py - MyProject"
    process_name: Optional[str] = None     # e.g. "code.exe"
    file_path: Optional[str] = None        # for file/screenshot events

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a flat dictionary for storage."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "category": self.category.value,
            "action": self.action.value,
            "application": self.application,
            "window_title": self.window_title,
            "process_name": self.process_name,
            "file_path": self.file_path,
            "data": str(self.data),  # stored as text in SQLite
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CaptureEvent":
        """Deserialize from a storage dictionary."""
        import ast
        data_raw = d.get("data", "{}")
        try:
            data = ast.literal_eval(data_raw) if isinstance(data_raw, str) else data_raw
        except (ValueError, SyntaxError):
            data = {"raw": data_raw}

        return cls(
            event_id=d["event_id"],
            timestamp=datetime.fromisoformat(d["timestamp"]),
            category=EventCategory(d["category"]),
            action=EventAction(d["action"]),
            application=d.get("application"),
            window_title=d.get("window_title"),
            process_name=d.get("process_name"),
            file_path=d.get("file_path"),
            data=data,
        )

    def __repr__(self) -> str:
        ts = self.timestamp.strftime("%H:%M:%S")
        return f"[{ts}] {self.category.value}/{self.action.value}: {self.summary}"

    @property
    def summary(self) -> str:
        """Human-readable one-line summary."""
        if self.action == EventAction.SCREENSHOT:
            return f"Screenshot saved"
        if self.action == EventAction.WINDOW_FOCUS:
            return f"{self.application or 'Unknown'} — {self.window_title or ''}"
        if self.action in (
            EventAction.FILE_CREATED,
            EventAction.FILE_MODIFIED,
            EventAction.FILE_DELETED,
        ):
            return f"{self.action.value}: {self.file_path or ''}"
        if self.action == EventAction.PROCESS_STARTED:
            return f"Started: {self.process_name or ''}"
        if self.action == EventAction.PROCESS_ENDED:
            return f"Ended: {self.process_name or ''}"
        if self.action == EventAction.CLIPBOARD_CHANGE:
            preview = str(self.data.get("preview", ""))[:60]
            return f"Clipboard: {preview}"
        if self.action == EventAction.STEP_MARKER:
            return f"📌 {self.data.get('label', 'Step marker')}"
        return self.action.value
