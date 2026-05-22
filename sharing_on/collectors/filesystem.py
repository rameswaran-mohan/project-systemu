"""File system change collector — watches directories for file modifications.

Uses `watchdog` for cross-platform inotify/FSEvents/ReadDirectoryChangesW support.
Captures file diffs for text files when possible.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from sharing_on.collectors.base import BaseCollector
from sharing_on.events.models import CaptureEvent, EventAction, EventCategory
from sharing_on.events.store import EventStore

logger = logging.getLogger(__name__)

# Maximum file size to capture content/diffs (1 MB)
MAX_FILE_SIZE = 1_048_576

# Extensions we treat as text (for diff capture)
TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".md", ".txt", ".rst", ".csv", ".xml", ".sql", ".sh", ".bash",
    ".bat", ".ps1", ".cmd", ".java", ".c", ".cpp", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".env", ".gitignore", ".dockerfile",
    "Makefile", "Dockerfile", ".tf", ".hcl",
}


class _FileChangeHandler(FileSystemEventHandler):
    """Watchdog event handler that forwards events to our collector."""

    def __init__(self, collector: "FileSystemCollector"):
        super().__init__()
        self._collector = collector

    def on_created(self, event):
        if not event.is_directory:
            self._collector._on_file_event(event.src_path, EventAction.FILE_CREATED)

    def on_modified(self, event):
        if not event.is_directory:
            self._collector._on_file_event(event.src_path, EventAction.FILE_MODIFIED)

    def on_deleted(self, event):
        if not event.is_directory:
            self._collector._on_file_event(event.src_path, EventAction.FILE_DELETED)

    def on_moved(self, event):
        if not event.is_directory:
            self._collector._on_file_event(
                event.src_path,
                EventAction.FILE_MOVED,
                extra={"dest_path": event.dest_path},
            )


class FileSystemCollector(BaseCollector):
    """Watches specified directories for file changes.

    For text files, captures unified diffs between the previous and current version.
    """

    name = "filesystem"

    def __init__(
        self,
        event_store: EventStore,
        watch_dirs: List[str],
        ignore_patterns: Optional[List[str]] = None,
    ):
        super().__init__(event_store)
        self._watch_dirs = [Path(d).resolve() for d in watch_dirs if Path(d).exists()]
        self._ignore_patterns = set(ignore_patterns or [])
        self._observer: Optional[Observer] = None
        # Cache file content hashes + snapshots for diff generation
        self._file_snapshots: Dict[str, str] = {}
        self._file_hashes: Dict[str, str] = {}

    def _collect_loop(self) -> None:
        if not self._watch_dirs:
            logger.warning("FileSystemCollector: no valid watch directories")
            return

        self._observer = Observer()
        handler = _FileChangeHandler(self)

        for watch_dir in self._watch_dirs:
            try:
                self._observer.schedule(handler, str(watch_dir), recursive=True)
                logger.info(f"Watching directory: {watch_dir}")
            except Exception as e:
                logger.warning(f"Cannot watch {watch_dir}: {e}")

        self._observer.start()

        # Keep the collector alive until stopped
        while self._running:
            time.sleep(0.5)

        self._observer.stop()
        self._observer.join(timeout=5.0)

    def stop(self) -> None:
        """Override to also stop the watchdog observer."""
        self._running = False
        if self._observer:
            self._observer.stop()
        super().stop()

    def _on_file_event(
        self,
        file_path: str,
        action: EventAction,
        extra: Optional[Dict] = None,
    ) -> None:
        """Handle a file system event from watchdog."""
        path = Path(file_path)

        # Skip ignored patterns
        if self._should_ignore(path):
            return

        data = extra or {}
        data["file_name"] = path.name
        data["file_extension"] = path.suffix

        # For modifications, try to capture a diff
        if action == EventAction.FILE_MODIFIED and self._is_text_file(path):
            diff = self._capture_diff(path)
            if diff:
                data["diff"] = diff

        # For creations, snapshot the file content
        if action == EventAction.FILE_CREATED and self._is_text_file(path):
            self._snapshot_file(path)

        self.emit(CaptureEvent(
            category=EventCategory.FILE,
            action=action,
            timestamp=datetime.now(timezone.utc),
            file_path=str(path),
            data=data,
        ))

    def _should_ignore(self, path: Path) -> bool:
        """Check if a file path matches any ignore patterns."""
        name = path.name
        parts = set(path.parts)

        for pattern in self._ignore_patterns:
            # Simple directory name match
            if not pattern.startswith("*") and pattern in parts:
                return True
            # Extension match (e.g., "*.pyc")
            if pattern.startswith("*") and name.endswith(pattern[1:]):
                return True

        return False

    def _is_text_file(self, path: Path) -> bool:
        """Check if a file is likely a text file we can diff."""
        if path.suffix.lower() in TEXT_EXTENSIONS:
            return True
        if path.name in {"Makefile", "Dockerfile", "Vagrantfile", ".gitignore"}:
            return True
        return False

    def _snapshot_file(self, path: Path) -> None:
        """Store a snapshot of file content for later diffing."""
        try:
            if path.exists() and path.stat().st_size <= MAX_FILE_SIZE:
                content = path.read_text(encoding="utf-8", errors="replace")
                key = str(path)
                self._file_snapshots[key] = content
                self._file_hashes[key] = hashlib.md5(
                    content.encode()
                ).hexdigest()
        except Exception:
            pass

    def _capture_diff(self, path: Path) -> Optional[str]:
        """Generate a unified diff between previous snapshot and current content."""
        key = str(path)
        try:
            if not path.exists() or path.stat().st_size > MAX_FILE_SIZE:
                return None

            new_content = path.read_text(encoding="utf-8", errors="replace")
            new_hash = hashlib.md5(new_content.encode()).hexdigest()

            # Skip if content hasn't actually changed
            if self._file_hashes.get(key) == new_hash:
                return None

            old_content = self._file_snapshots.get(key, "")
            old_lines = old_content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)

            diff = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{path.name}",
                tofile=f"b/{path.name}",
                lineterm="",
            )
            diff_text = "\n".join(diff)

            # Update snapshot
            self._file_snapshots[key] = new_content
            self._file_hashes[key] = new_hash

            # Only return non-empty diffs, truncated to 5000 chars
            if diff_text.strip():
                return diff_text[:5000]
            return None

        except Exception as e:
            logger.debug(f"Diff capture failed for {path}: {e}")
            return None
