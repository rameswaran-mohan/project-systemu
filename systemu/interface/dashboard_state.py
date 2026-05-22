"""Systemu NiceGUI Web Dashboard — shared state and helpers.

This module holds:
  - The global AppState singleton (all backend interfaces + config)
  - AppState.create() factory — picks the right backend from SYSTEMU_STORAGE
  - Shared NiceGUI theme constants (colors, spacing)
  - Refresh helpers for all pages

Backend modes (set via SYSTEMU_STORAGE env var):
  "file"    — original JSON file vault + in-memory EventBus + thread Supervisor
               (default; zero external dependencies; backwards-compatible)
  "sqlite"  — SQLite vault + SQLite Huey queue + SQLite event store
               (Phase 1/2: hobbyist docker-compose, cross-process, crash-resilient)
  "postgres"— PostgreSQL vault + Redis Huey queue + Redis Streams
               (Phase 4: production / enterprise)
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from sharing_on.config import Config

from systemu.abstractions import IApprovalGate, IEventBroker, ITaskQueue, IVault

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Global app state
# ─────────────────────────────────────────────────────────────────────────────

class AppState:
    """Singleton carrying all backend interface references across NiceGUI pages.

    Use AppState.create(config) to build the correct backend from the
    SYSTEMU_STORAGE environment variable.  All pages call AppState.get()
    to access vault, queue, events, and approvals — never the concrete classes.
    """

    _instance: Optional["AppState"] = None
    _lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        config: Config,
        vault: IVault,
        queue: ITaskQueue,
        events: IEventBroker,
        approvals: IApprovalGate,
    ) -> None:
        self.config    = config
        self.vault     = vault        # IVault — all entity CRUD
        self.queue     = queue        # ITaskQueue — submit / status
        self.events    = events       # IEventBroker — pub/sub + approval gate
        self.approvals = approvals    # IApprovalGate — log_event / notify_user / confirm

        # Resolve project root once — absolute, survives cwd changes in subprocess
        try:
            import systemu as _systemu_pkg
            self._project_root = str(Path(_systemu_pkg.__file__).parent.parent.absolute())
        except Exception:
            self._project_root = str(Path(config.vault_dir).parent.absolute())

        AppState._instance = self

    @property
    def project_root(self) -> str:
        """Absolute path to the project root (e.g. .../Project_Systemu)."""
        return self._project_root

    @classmethod
    def get(cls) -> "AppState":
        if cls._instance is None:
            raise RuntimeError(
                "AppState not initialised. Call AppState.create(config) first."
            )
        return cls._instance

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, config: Config) -> "AppState":
        """Build AppState with the appropriate backend for SYSTEMU_STORAGE.

        Environment variable SYSTEMU_STORAGE:
          "file"     (default) — original JSON vault + in-memory EventBus
          "sqlite"             — SQLAlchemy SQLite vault + Huey SqliteHuey
          "postgres"           — PostgreSQL vault + Redis Huey + Redis Streams

        Returns the AppState singleton (idempotent if called twice).
        Thread-safe: double-checked locking prevents duplicate initialisation
        when the dashboard thread and any other thread race on startup.
        """
        if cls._instance is not None:
            return cls._instance

        with cls._lock:
            # Re-check inside the lock — another thread may have won the race.
            if cls._instance is not None:
                return cls._instance

            mode = os.environ.get("SYSTEMU_STORAGE", "file").lower()
            logger.info("[AppState] Creating backend — SYSTEMU_STORAGE=%s", mode)

            if mode == "file":
                return cls._create_file_backend(config)
            elif mode == "sqlite":
                return cls._create_sqlite_backend(config)
            elif mode == "postgres":
                return cls._create_postgres_backend(config)
            elif mode == "parallel":
                return cls._create_parallel_backend(config)
            else:
                logger.warning(
                    "[AppState] Unknown SYSTEMU_STORAGE=%r — falling back to 'file'", mode
                )
                return cls._create_file_backend(config)

    # ── File backend (current default) ────────────────────────────────────────

    @classmethod
    def _create_file_backend(cls, config: Config) -> "AppState":
        """Wire the original file-based Vault + in-memory EventBus + Supervisor."""
        from systemu.vault.vault import Vault as _RawVault
        from systemu.storage.file_vault import FileVault
        from systemu.interface.event_bus import EventBus
        from systemu.events.memory_event_broker import MemoryEventBroker
        import systemu.interface.notifications as _notif
        from systemu.approval.notification_gate import NotificationApprovalGate

        raw_vault = _RawVault(config.vault_dir)
        vault     = FileVault(raw_vault)

        # Inject vault into notifications module (preserves existing behaviour)
        _notif.set_vault(raw_vault)

        events    = MemoryEventBroker(EventBus.get())
        approvals = NotificationApprovalGate(_notif)

        # Supervisor is started here; queue wraps it
        try:
            from systemu.runtime.supervisor import Supervisor
            from systemu.queue.thread_task_queue import ThreadTaskQueue
            sup   = Supervisor.init(config, raw_vault)
            queue = ThreadTaskQueue(sup)
        except Exception as exc:
            logger.warning(
                "[AppState] Supervisor failed to start (non-fatal): %s", exc
            )
            queue = _NoOpTaskQueue()  # type: ignore[assignment]

        state = cls(config, vault, queue, events, approvals)
        logger.info("[AppState] File backend ready.")
        return state

    # ── SQLite backend (Phase 1/2 — hobbyist docker-compose) ─────────────────

    @classmethod
    def _create_sqlite_backend(cls, config: Config) -> "AppState":
        """Wire SQLite vault + Huey SqliteHuey + SqliteEventBroker (Phase 3)."""
        try:
            from systemu.storage.sqlite.vault import SqliteVault
            from systemu.interface.event_bus import EventBus
            from systemu.events.sqlite_event_broker import SqliteEventBroker
            from systemu.approval.sqlite_approval_gate import SqliteApprovalGate

            # Allow override via SYSTEMU_DATABASE_URL (e.g. set by docker-compose)
            db_url = os.environ.get(
                "SYSTEMU_DATABASE_URL",
                f"sqlite:///{Path(config.vault_dir).parent / 'data' / 'systemu.db'}",
            )
            # Ensure the data directory exists for file-based SQLite URLs
            if db_url.startswith("sqlite:///"):
                raw_path = db_url[len("sqlite:///"):]
                Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
            elif db_url.startswith("sqlite:////"):
                raw_path = "/" + db_url[len("sqlite:////"):]
                Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

            vault  = SqliteVault(db_url)

            # Phase 3: cross-process event broker (polls DB every 2 s for remote events)
            events = SqliteEventBroker(db_url, local_bus=EventBus.get())

            # Phase 3: approval gate routes notify_user() through the DB approval gate
            approvals = SqliteApprovalGate(broker=events, vault=vault)

                # Always start the Supervisor using the SQLite vault so that
            # shadow execution works even when no external Huey consumer is
            # running.  The Supervisor thread-pool handles all in-process
            # shadow runs; HueyTaskQueue (when available) is used for
            # other async tasks.
            from systemu.runtime.supervisor import Supervisor
            from systemu.queue.thread_task_queue import ThreadTaskQueue
            sup   = Supervisor.init(config, vault)   # uses SqliteVault — matches CLI writes
            queue = ThreadTaskQueue(sup)

            # Huey SqliteHuey task queue (Phase 2) — optional overlay.
            # If available, wraps HueyTaskQueue around the running Supervisor
            # so external huey_consumer workers can also submit tasks.
            try:
                from systemu.queue.huey_task_queue import HueyTaskQueue
                huey_queue = HueyTaskQueue.create_sqlite(db_url)
                logger.info("[AppState] HueyTaskQueue available — Supervisor + Huey both active")
            except (ImportError, Exception) as exc:
                logger.debug("[AppState] HueyTaskQueue unavailable (%s) — Supervisor-only mode", exc)

            state = cls(config, vault, queue, events, approvals)
            logger.info("[AppState] SQLite backend ready — %s", db_url)
            return state

        except Exception as exc:
            logger.error(
                "[AppState] SQLite backend failed (%s) — falling back to file", exc
            )
            return cls._create_file_backend(config)

    # ── PostgreSQL backend (Phase 4 — production/enterprise) ─────────────────

    @classmethod
    def _create_postgres_backend(cls, config: Config) -> "AppState":
        """Wire PostgreSQL vault + Redis Huey + Redis Streams event broker.

        Accepts either prefixed (SYSTEMU_DATABASE_URL / SYSTEMU_REDIS_URL)
        or bare (DATABASE_URL / REDIS_URL) env names.  Compose + install.py
        write the prefixed form; older deployments wrote the bare names.
        """
        database_url = (
            os.environ.get("SYSTEMU_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
        )
        redis_url = (
            os.environ.get("SYSTEMU_REDIS_URL")
            or os.environ.get("REDIS_URL")
        )

        # the Redis URL is only required when the Huey queue
        # broker is Redis (i.e. docker-enterprise).  docker-local uses
        # Huey-SQLite + Postgres with no Redis container — the original
        # check rejected that combo and silently fell back to FileVault
        # while the worker continued writing to Postgres.  Dashboard and
        # worker ended up on different backends.  See
        # captures/E2E_VERDICT_DOCKER.md finding A for the repro.
        queue_broker = (os.environ.get("SYSTEMU_QUEUE_BROKER") or "sqlite").lower()
        needs_redis  = (queue_broker == "redis")

        missing: list[str] = []
        if not database_url:
            missing.append("SYSTEMU_DATABASE_URL/DATABASE_URL")
        if needs_redis and not redis_url:
            missing.append("SYSTEMU_REDIS_URL/REDIS_URL")

        if missing:
            logger.error(
                "[AppState] postgres mode requires %s — falling back to file",
                " and ".join(missing),
            )
            return cls._create_file_backend(config)

        try:
            from systemu.storage.sqlite.vault import SqliteVault  # reuses SA models
            from systemu.events.memory_event_broker import MemoryEventBroker
            from systemu.interface.event_bus import EventBus
            import systemu.interface.notifications as _notif
            from systemu.approval.notification_gate import NotificationApprovalGate

            vault     = SqliteVault(database_url)   # SA handles both sqlite:// and postgresql://
            events    = MemoryEventBroker(EventBus.get())  # replaced by RedisEventBroker in Phase 4
            _notif.set_vault(vault)
            approvals = NotificationApprovalGate(_notif)

            from systemu.queue.thread_task_queue import ThreadTaskQueue
            from systemu.runtime.supervisor import Supervisor
            sup   = Supervisor.init(config, vault)   # uses postgres vault — matches CLI writes
            queue = ThreadTaskQueue(sup)

            state = cls(config, vault, queue, events, approvals)
            logger.info("[AppState] PostgreSQL backend ready.")
            return state

        except Exception as exc:
            logger.error(
                "[AppState] PostgreSQL backend failed (%s) — falling back to file", exc
            )
            return cls._create_file_backend(config)

    # ── Parallel backend (migration mode — dual-write) ────────────────────────

    @classmethod
    def _create_parallel_backend(cls, config: Config) -> "AppState":
        """Wire FileVault (primary) + SqliteVault (secondary) behind ParallelVault.

        Use SYSTEMU_STORAGE=parallel during the file → SQLite migration window.
        All writes go to both vaults; reads come from FileVault (authoritative).
        Mismatches between the two are logged as WARNINGs for validation.

        Set SYSTEMU_DATABASE_URL or let it default to <vault_dir>/../data/systemu.db.
        """
        try:
            from pathlib import Path
            from systemu.vault.vault import Vault as _RawVault
            from systemu.storage.file_vault import FileVault
            from systemu.storage.sqlite.vault import SqliteVault
            from systemu.storage.parallel_vault import ParallelVault
            from systemu.interface.event_bus import EventBus
            from systemu.events.memory_event_broker import MemoryEventBroker
            import systemu.interface.notifications as _notif
            from systemu.approval.notification_gate import NotificationApprovalGate

            raw_vault = _RawVault(config.vault_dir)
            primary   = FileVault(raw_vault)

            db_url = os.environ.get(
                "SYSTEMU_DATABASE_URL",
                f"sqlite:///{Path(config.vault_dir).parent / 'data' / 'systemu.db'}",
            )
            secondary = SqliteVault(db_url)
            vault     = ParallelVault(primary, secondary)

            # Notifications write event_log to the file vault's path (primary is auth.)
            _notif.set_vault(raw_vault)
            events    = MemoryEventBroker(EventBus.get())
            approvals = NotificationApprovalGate(_notif)

            try:
                from systemu.runtime.supervisor import Supervisor
                from systemu.queue.thread_task_queue import ThreadTaskQueue
                sup   = Supervisor.init(config, raw_vault)
                queue = ThreadTaskQueue(sup)
            except Exception as exc:
                logger.warning("[AppState] Supervisor failed in parallel mode: %s", exc)
                queue = _NoOpTaskQueue()  # type: ignore[assignment]

            state = cls(config, vault, queue, events, approvals)
            logger.info(
                "[AppState] Parallel backend ready — primary=file, secondary=%s", db_url
            )
            return state

        except Exception as exc:
            logger.error(
                "[AppState] Parallel backend failed (%s) — falling back to file", exc
            )
            return cls._create_file_backend(config)


# ─────────────────────────────────────────────────────────────────────────────
#  No-op fallback task queue (used when Supervisor fails to start)
# ─────────────────────────────────────────────────────────────────────────────

class _NoOpTaskQueue:
    """Fallback ITaskQueue that logs but never executes anything.
    Used only when the Supervisor fails to initialise (test/dry-run contexts).
    """

    def submit(self, activity_id, shadow_id, *, priority=5, reason="manual", retry_count=0):
        logger.warning("[NoOpQueue] submit called but queue not initialised — ignoring.")
        return "noop"

    def get_status(self):
        return {"queue_depth": 0, "running_count": 0, "running": [],
                "dead_letters": [], "dead_letter_count": 0, "max_concurrent": 0}

    def shutdown(self):
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Theme constants (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

THEME = {
    # Base palette
    "bg":           "#0f1117",
    "surface":      "#1a1d27",
    "surface2":     "#22263a",
    "border":       "#2d3148",
    "text":         "#e8eaf6",
    "text_muted":   "#6b7280",

    # Accent
    "primary":      "#6366f1",   # indigo-500
    "primary_dim":  "#4f52c8",
    "success":      "#22c55e",
    "warning":      "#f59e0b",
    "danger":       "#ef4444",
    "info":         "#38bdf8",

    # Status colours
    "status_colors": {
        "pending_approval":  "#f59e0b",
        "approved":          "#22c55e",
        "linked":            "#6366f1",
        "proposed":          "#f59e0b",
        "forged":            "#38bdf8",
        "deployed":          "#22c55e",
        "awakened":          "#6366f1",
        "dormant":           "#6b7280",
        "retired":           "#ef4444",
        "unassigned":        "#f59e0b",
        "assigned":          "#6366f1",
        "partial":           "#f97316",
    },
}

GLOBAL_CSS = f"""
:root {{
    --bg:           {THEME['bg']};
    --surface:      {THEME['surface']};
    --surface2:     {THEME['surface2']};
    --border:       {THEME['border']};
    --text:         {THEME['text']};
    --text-muted:   {THEME['text_muted']};
    --primary:      {THEME['primary']};
    --success:      {THEME['success']};
    --warning:      {THEME['warning']};
    --danger:       {THEME['danger']};
    --info:         {THEME['info']};
}}

body, html {{
    background: var(--bg) !important;
    color: var(--text) !important;
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, sans-serif;
    margin: 0;
}}

.nicegui-content {{
    background: var(--bg) !important;
    min-height: 100vh;
}}

/* Card */
.s-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    transition: border-color 0.2s, box-shadow 0.2s;
}}
.s-card:hover {{
    border-color: var(--primary);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--primary) 30%, transparent);
}}

/* Stat card */
.stat-card {{
    background: linear-gradient(135deg, var(--surface) 60%, var(--surface2));
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    min-width: 160px;
    text-align: center;
}}

/* Badge */
.badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}}

/* Table */
.s-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 14px;
}}
.s-table th {{
    background: var(--surface2);
    color: var(--text-muted);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    text-align: left;
}}
.s-table td {{
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    color: var(--text);
}}
.s-table tr:hover td {{
    background: var(--surface2);
}}

/* Nav sidebar */
.nav-link {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    border-radius: 8px;
    cursor: pointer;
    color: var(--text-muted);
    font-weight: 500;
    transition: background 0.15s, color 0.15s;
    text-decoration: none;
}}
.nav-link:hover, .nav-link.active {{
    background: color-mix(in srgb, var(--primary) 15%, transparent);
    color: var(--text);
}}

/* Button overrides */
.q-btn.primary-btn {{
    background: var(--primary) !important;
    color: white !important;
    border-radius: 8px;
}}
.q-btn.danger-btn {{
    background: var(--danger) !important;
    color: white !important;
    border-radius: 8px;
}}
.q-btn.success-btn {{
    background: var(--success) !important;
    color: white !important;
    border-radius: 8px;
}}

/* Responsive sidebar — collapses to icons on narrow viewports.
   The sidebar element gets .s-sidebar; the brand text + nav-link labels
   get .s-sidebar-label so they hide on collapse.  On narrow viewports a
   .s-sidebar-toggle hamburger button appears top-left; tapping it adds
   .s-sidebar-open to <body>, which:
     * expands the sidebar back to full width
     * shows a backdrop overlay so the sidebar floats above content
     * reveals the labels again
   Tapping the backdrop dismisses. */

/* Hamburger button — hidden on desktop, fixed top-left on mobile. */
.s-sidebar-toggle {{
    display: none;
    position: fixed;
    top: 12px;
    left: 12px;
    z-index: 1100;
    width: 36px;
    height: 36px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 18px;
    cursor: pointer;
    transition: background 0.15s;
}}
.s-sidebar-toggle:hover {{
    background: var(--surface2);
}}

/* Backdrop — hidden by default, shown when sidebar is open on mobile. */
.s-sidebar-backdrop {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.5);
    z-index: 999;
    cursor: pointer;
}}

@media (max-width: 768px) {{
    .s-sidebar {{
        width: 64px !important;
        min-width: 64px !important;
        padding-left: 4px !important;
        padding-right: 4px !important;
        z-index: 1000;
        transition: width 0.2s ease, min-width 0.2s ease;
    }}
    .s-sidebar-label,
    .s-sidebar-footer {{
        display: none !important;
    }}
    .s-sidebar-toggle {{
        display: block;
    }}

    /* Tap the hamburger -> body gets .s-sidebar-open, sidebar expands. */
    body.s-sidebar-open .s-sidebar {{
        width: 220px !important;
        min-width: 220px !important;
        padding-left: 12px !important;
        padding-right: 12px !important;
    }}
    body.s-sidebar-open .s-sidebar-label,
    body.s-sidebar-open .s-sidebar-footer {{
        display: revert !important;
    }}
    body.s-sidebar-open .s-sidebar-backdrop {{
        display: block;
    }}
}}
"""


def status_badge_html(status: str) -> str:
    """Return a coloured HTML badge for a status string."""
    color = THEME["status_colors"].get(status.lower(), "#6b7280")
    return (
        f'<span class="badge" style="background: color-mix(in srgb, {color} 20%, transparent); '
        f'color: {color}; border: 1px solid color-mix(in srgb, {color} 40%, transparent);">'
        f'{status}</span>'
    )
