"""SqliteEventBroker — IEventBroker backed by SQLAlchemy for cross-process events.

Bridges worker-published events to the dashboard process so the activity log
and real-time progress view stay live even when shadow execution runs in a
separate container.

Architecture
------------
Both the dashboard and the worker create a SqliteEventBroker pointing at the
same DB file.  Each instance has a unique ``instance_id`` (pid + random hex).

  Worker process                        Dashboard process
  ──────────────────────────────────    ─────────────────────────────────────
  broker.publish(event)                 broker.subscribe(callback)
    → local EventBus (no subscribers)   ← events from local EventBus
    → INSERT INTO events (source=self)
                                        Background poll thread (every 2 s):
                                          SELECT id, payload FROM events
                                          WHERE source != self AND id > watermark
                                          → local EventBus.publish(payload)
                                          → watermark = max(id)

Approval gate (IEventBroker.request_approval / resolve_approval)
----------------------------------------------------------------
  Worker: request_approval(...)
    → INSERT INTO approvals (status="pending")
    → blocks on SELECT poll until status="resolved" or timeout

  Dashboard: resolve_approval(request_id, choice)
    → UPDATE approvals SET status="resolved", choice=choice

  Dashboard UI (approval page) calls resolve_approval via AppState.events.

Usage
-----
    from systemu.events.sqlite_event_broker import SqliteEventBroker
    from systemu.interface.event_bus import EventBus

    broker = SqliteEventBroker(
        "sqlite:///data/systemu.db",
        local_bus=EventBus.get(),
    )
    # broker polls DB automatically in a daemon thread.
    # Shut down cleanly:
    broker.stop()
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime

from systemu.core.utils import utcnow
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker, Session

from systemu.storage.sqlite.models import ApprovalRow, Base, EventRow

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Engine factory (shared between broker and gate in the same process)
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine(db_url: str):
    connect_args: dict = {}
    is_sqlite = db_url.startswith("sqlite")
    if is_sqlite:
        connect_args["check_same_thread"] = False

    eng = create_engine(
        db_url,
        connect_args=connect_args,
        pool_pre_ping=True,
        echo=False,
        future=True,
    )

    if is_sqlite:
        @event.listens_for(eng, "connect")
        def _set_pragmas(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return eng


# ─────────────────────────────────────────────────────────────────────────────
#  SqliteEventBroker
# ─────────────────────────────────────────────────────────────────────────────

class SqliteEventBroker:
    """IEventBroker that persists events to SQLite for cross-process delivery.

    Args:
        db_url:         SQLAlchemy URL for the shared DB.
        local_bus:      In-process EventBus instance used for subscriptions and
                        local publish fanout.
        poll_interval_s: How often the bridge thread polls for remote events.
                         Default 2 s — low latency vs. SQLite write overhead.
    """

    def __init__(
        self,
        db_url: str,
        local_bus: Any,
        *,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._local_bus      = local_bus
        self._poll_interval  = poll_interval_s
        # Unique per-process identifier used to filter own events in the poller.
        self._instance_id    = f"proc-{os.getpid()}-{uuid.uuid4().hex[:6]}"

        self._engine  = _make_engine(db_url)
        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)

        # Create Phase 3 tables (EventRow, ApprovalRow) if they don't exist yet.
        # SqliteVault.create_all() handles the entity tables; we extend here.
        Base.metadata.create_all(self._engine, checkfirst=True)

        # Watermark: start from the highest existing event id so we only bridge
        # NEW events going forward (not replaying the entire history on start-up).
        self._watermark = self._get_max_event_id()

        # Background bridge thread — daemon so it dies with the process.
        self._stop_event   = threading.Event()
        self._bridge_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="SqliteEventBroker-bridge",
        )
        self._bridge_thread.start()
        logger.info(
            "[SqliteEventBroker] Ready — instance=%s watermark=%d poll=%.1fs",
            self._instance_id, self._watermark, poll_interval_s,
        )

    # ── Session helper ────────────────────────────────────────────────────────

    def _session(self) -> Session:
        return self._Session()

    # ── Watermark bootstrap ───────────────────────────────────────────────────

    def _get_max_event_id(self) -> int:
        try:
            with self._session() as s:
                row = s.execute(
                    select(EventRow.id).order_by(EventRow.id.desc()).limit(1)
                ).scalar()
            return row or 0
        except Exception:
            return 0

    # ── Bridge poll loop ──────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Daemon thread: poll for remote events and bridge them to the local bus."""
        while not self._stop_event.is_set():
            try:
                self._bridge_new_events()
            except Exception as exc:
                logger.debug("[SqliteEventBroker] Bridge poll error: %s", exc)
            self._stop_event.wait(self._poll_interval)

    def _bridge_new_events(self) -> None:
        """Read events with id > watermark that weren't published by this process."""
        with self._session() as s:
            rows = s.execute(
                select(EventRow)
                .where(EventRow.id > self._watermark)
                .where(EventRow.source != self._instance_id)
                .order_by(EventRow.id.asc())
                .limit(100)           # batch cap to avoid huge replays
            ).scalars().all()
            payloads = [(r.id, r.payload) for r in rows]

        for row_id, payload in payloads:
            try:
                # Publish to local bus WITHOUT writing back to DB (avoid ping-pong)
                self._local_bus.publish(payload)
            except Exception as exc:
                logger.warning("[SqliteEventBroker] Failed to bridge event %d: %s", row_id, exc)
            self._watermark = row_id

    # ── IEventBroker: publish / subscribe / buffer ────────────────────────────

    def publish(self, event_dict: Dict[str, Any]) -> None:
        """Publish to local bus AND persist to DB for cross-process delivery."""
        # Local delivery first — zero latency for same-process subscribers
        self._local_bus.publish(event_dict)
        # Persist for bridge (worker→dashboard or dashboard→worker)
        self._write_event(event_dict)

    def _write_event(self, payload: Dict[str, Any]) -> None:
        try:
            with self._session() as s:
                s.add(EventRow(
                    ts=utcnow(),
                    source=self._instance_id,
                    payload=payload,
                ))
                s.commit()
        except Exception as exc:
            logger.warning("[SqliteEventBroker] Failed to persist event: %s", exc)

    def subscribe(
        self,
        callback: Callable[[Dict[str, Any]], None],
        *,
        replay: bool = True,
    ) -> Callable[[], None]:
        """Subscribe to events — delegates entirely to the local in-memory bus."""
        return self._local_bus.subscribe(callback, replay=replay)

    def get_buffer(self) -> List[Dict[str, Any]]:
        """Return the local event buffer (recent events for replay on new subscribers)."""
        return self._local_bus.get_buffer()

    # ── IEventBroker: convenience publishers ─────────────────────────────────

    def publish_supervisor(
        self,
        message: str,
        *,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._local_bus.publish_supervisor(message, level=level, context=context)
        # publish_supervisor writes to local bus which calls self.publish internally
        # via the bus's own mechanism.  We write to DB separately to capture it.
        # (If the local bus already called self.publish, DB write is in publish()).
        # Since MemoryEventBroker.publish_supervisor calls self._bus.publish_supervisor
        # which in turn calls self._bus.publish — and our publish() override is NOT
        # on the inner bus — we must explicitly persist here too.
        self._write_event({
            "ts":       utcnow().isoformat() + "Z",
            "level":    level.upper(),
            "category": "supervisor",
            "message":  message,
            "context":  context or {},
        })

    def publish_shadow(
        self,
        message: str,
        shadow_id: str,
        execution_id: str,
        *,
        level: str = "INFO",
        context: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._local_bus.publish_shadow(
            message, shadow_id, execution_id, level=level, context=context
        )
        self._write_event({
            "ts":           utcnow().isoformat() + "Z",
            "level":        level.upper(),
            "category":     "shadow",
            "message":      message,
            "shadow_id":    shadow_id,
            "execution_id": execution_id,
            "context":      context or {},
        })

    # ── IEventBroker: approval gate ───────────────────────────────────────────

    def request_approval(
        self,
        request_id: str,
        title: str,
        message: str,
        options: List[str],
        *,
        context: Optional[Dict[str, Any]] = None,
        timeout_s: float = 120.0,
        default: str = "",
    ) -> str:
        """Write an approval request to DB and block-poll until resolved or timeout.

        Returns the chosen option string.  Falls back to `default` (or first option)
        on timeout.
        """
        resolved_default = default or (options[0] if options else "")

        # Write pending approval row
        with self._session() as s:
            s.merge(ApprovalRow(
                request_id=request_id,
                title=title,
                message=message,
                options=options,
                context=context or {},
                status="pending",
                default=resolved_default,
                timeout_s=timeout_s,
                created_at=utcnow(),
            ))
            s.commit()

        # Publish event so dashboard UI re-renders the approvals list
        self.publish({
            "ts":        utcnow().isoformat() + "Z",
            "level":     "INFO",
            "category":  "approval",
            "message":   f"Approval requested: {title}",
            "context":   {"request_id": request_id, "options": options},
        })

        # Block-poll
        deadline = time.time() + timeout_s
        poll_interval = 2.0
        while time.time() < deadline:
            choice = self._get_approval_choice(request_id)
            if choice is not None:
                logger.info(
                    "[SqliteEventBroker] Approval %s resolved: %r", request_id, choice
                )
                return choice
            remaining = deadline - time.time()
            time.sleep(min(poll_interval, max(remaining, 0)))

        # Timeout
        logger.warning(
            "[SqliteEventBroker] Approval %s timed out after %.0fs — using default %r",
            request_id, timeout_s, resolved_default,
        )
        self._mark_approval_timed_out(request_id, resolved_default)
        return resolved_default

    def resolve_approval(self, request_id: str, choice: str) -> bool:
        """Resolve a pending approval request (called from the dashboard UI).

        Returns True if the request existed and was updated, False otherwise.
        """
        with self._session() as s:
            row = s.get(ApprovalRow, request_id)
            if row is None or row.status != "pending":
                return False
            row.status      = "resolved"
            row.choice      = choice
            row.resolved_at = utcnow()
            s.commit()
        self.publish({
            "ts":       utcnow().isoformat() + "Z",
            "level":    "INFO",
            "category": "approval",
            "message":  f"Approval resolved: {request_id} -> {choice!r}",
            "context":  {"request_id": request_id, "choice": choice},
        })
        return True

    def has_pending_approval(self, request_id: str) -> bool:
        with self._session() as s:
            row = s.get(ApprovalRow, request_id)
        return row is not None and row.status == "pending"

    def list_pending_approvals(self) -> List[str]:
        with self._session() as s:
            rows = s.execute(
                select(ApprovalRow.request_id)
                .where(ApprovalRow.status == "pending")
            ).scalars().all()
        return list(rows)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_approval_choice(self, request_id: str) -> Optional[str]:
        try:
            with self._session() as s:
                row = s.get(ApprovalRow, request_id)
            if row and row.status == "resolved":
                return row.choice
        except Exception as exc:
            logger.debug("[SqliteEventBroker] approval poll error: %s", exc)
        return None

    def _mark_approval_timed_out(self, request_id: str, default: str) -> None:
        try:
            with self._session() as s:
                row = s.get(ApprovalRow, request_id)
                if row and row.status == "pending":
                    row.status      = "timed_out"
                    row.choice      = default
                    row.resolved_at = utcnow()
                    s.commit()
        except Exception as exc:
            logger.debug("[SqliteEventBroker] could not mark approval timed_out: %s", exc)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Signal the bridge thread to stop and dispose the engine.

        Call on application shutdown to release DB connections cleanly.
        """
        self._stop_event.set()
        self._bridge_thread.join(timeout=5.0)
        self._engine.dispose()
        logger.info("[SqliteEventBroker] Stopped.")
