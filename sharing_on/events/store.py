"""SQLite-backed event store with thread-safe queue-based writes."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from sharing_on.events.models import CaptureEvent, EventAction, EventCategory


class EventStore:
    """Thread-safe event storage backed by SQLite.

    Multiple collector threads put events onto a queue.
    A single writer thread drains the queue into SQLite.
    This avoids any SQLite threading issues.
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._queue: queue.Queue[Optional[CaptureEvent]] = queue.Queue()
        self._writer_thread: Optional[threading.Thread] = None
        self._running = False
        self._event_count = 0
        self._lock = threading.Lock()

    @property
    def event_count(self) -> int:
        with self._lock:
            return self._event_count

    def start(self) -> None:
        """Initialize the database and start the writer thread."""
        self._init_db()
        self._running = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="event-writer"
        )
        self._writer_thread.start()

    def put(self, event: CaptureEvent) -> None:
        """Enqueue an event for writing (thread-safe, non-blocking)."""
        self._queue.put(event)

    def stop(self) -> None:
        """Stop the writer thread and flush remaining events."""
        self._running = False
        # Sentinel to unblock the queue.get()
        self._queue.put(None)
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=10.0)

    def get_all_events(self) -> List[CaptureEvent]:
        """Read all events from the database, ordered by timestamp."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY timestamp ASC"
            ).fetchall()
            return [self._row_to_event(row) for row in rows]
        finally:
            conn.close()

    def get_events_by_category(self, category: EventCategory) -> List[CaptureEvent]:
        """Get all events of a specific category."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE category = ? ORDER BY timestamp ASC",
                (category.value,),
            ).fetchall()
            return [self._row_to_event(row) for row in rows]
        finally:
            conn.close()

    # --- Internal ---

    def _init_db(self) -> None:
        """Create the events table if it doesn't exist."""
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id    TEXT PRIMARY KEY,
                timestamp   TEXT NOT NULL,
                category    TEXT NOT NULL,
                action      TEXT NOT NULL,
                application TEXT,
                window_title TEXT,
                process_name TEXT,
                file_path   TEXT,
                data        TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_cat ON events(category)"
        )
        conn.commit()
        conn.close()

    def _writer_loop(self) -> None:
        """Consume events from the queue and write them to SQLite."""
        conn = sqlite3.connect(str(self._db_path))
        batch: List[CaptureEvent] = []
        batch_size = 50

        while self._running or not self._queue.empty():
            try:
                event = self._queue.get(timeout=0.5)
                if event is None:
                    # Sentinel — flush and exit
                    break
                batch.append(event)

                # Flush in batches for performance
                if len(batch) >= batch_size:
                    self._write_batch(conn, batch)
                    batch.clear()
            except queue.Empty:
                # Flush partial batch on timeout
                if batch:
                    self._write_batch(conn, batch)
                    batch.clear()

        # Final flush
        if batch:
            self._write_batch(conn, batch)
        conn.close()

    def _write_batch(self, conn: sqlite3.Connection, batch: List[CaptureEvent]) -> None:
        """Write a batch of events to the database."""
        for event in batch:
            try:
                data_json = json.dumps(event.data, default=str)
            except (TypeError, ValueError):
                data_json = str(event.data)

            conn.execute(
                """INSERT OR IGNORE INTO events
                   (event_id, timestamp, category, action,
                    application, window_title, process_name, file_path, data)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.timestamp.isoformat(),
                    event.category.value,
                    event.action.value,
                    event.application,
                    event.window_title,
                    event.process_name,
                    event.file_path,
                    data_json,
                ),
            )
        conn.commit()
        with self._lock:
            self._event_count += len(batch)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> CaptureEvent:
        """Convert a database row back to a CaptureEvent."""
        data_raw = row["data"]
        try:
            data = json.loads(data_raw) if data_raw else {}
        except (json.JSONDecodeError, TypeError):
            data = {"raw": data_raw}

        return CaptureEvent(
            event_id=row["event_id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            category=EventCategory(row["category"]),
            action=EventAction(row["action"]),
            application=row["application"],
            window_title=row["window_title"],
            process_name=row["process_name"],
            file_path=row["file_path"],
            data=data,
        )
