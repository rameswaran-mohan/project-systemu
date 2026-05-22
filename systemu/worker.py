"""Systemu background worker entrypoint.

This module is invoked by the docker-compose worker service:
    python -m systemu.worker

Routing matrix:
  SYSTEMU_QUEUE=huey + SYSTEMU_QUEUE_BROKER=redis  → Huey RedisHuey consumer
  SYSTEMU_QUEUE=huey + SYSTEMU_QUEUE_BROKER=sqlite → Huey SqliteHuey consumer
  SYSTEMU_STORAGE=file (no Huey opt-in)            → Supervisor in-memory queue
  SYSTEMU_STORAGE in (sqlite, postgres) (no Huey)  → Supervisor + SQLite durable queue

Environment variables:
  SYSTEMU_MODE          — "local" | "docker-local" | "docker-enterprise" (informational)
  SYSTEMU_STORAGE       — "file" | "sqlite" | "postgres" (default: "file")
  SYSTEMU_DATABASE_URL  — SQLAlchemy URL for sqlite/postgres modes
  SYSTEMU_VAULT_DIR     — path to JSON vault (file mode; default: ./systemu/vault)
  SYSTEMU_QUEUE         — "" (default Supervisor) | "huey" (opt-in Huey consumer)
  SYSTEMU_QUEUE_BROKER  — "sqlite" (default) | "redis"
  SYSTEMU_REDIS_URL     — required when SYSTEMU_QUEUE_BROKER=redis
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Load .env before importing systemu modules that snapshot env at import.
# Same rationale as sharing_on/cli.py — without this, `python -m
# systemu.worker` in local mode picks file backend defaults regardless of
# what install.py wrote to .env.  Docker uses compose's env_file: so this
# is a no-op there.
try:
    from dotenv import load_dotenv as _load_dotenv
    _here = Path(__file__).resolve().parent
    for _candidate in (_here, _here.parent, _here.parent.parent):
        _env_path = _candidate / ".env"
        if _env_path.exists():
            _load_dotenv(_env_path, override=False)
            break
except ImportError:
    pass

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    stream=sys.stdout,
)


def _load_config():
    """Load sharing_on Config from environment / .env."""
    from sharing_on.config import Config
    return Config()


def run_file_worker() -> None:
    """Run the Supervisor in the foreground (file backend, in-memory queue)."""
    config = _load_config()
    from systemu.vault.vault import Vault
    from systemu.runtime.supervisor import Supervisor

    vault = Vault(config.vault_dir)
    sup   = Supervisor.init(config, vault)
    logger.info("[Worker] File-backend Supervisor started — vault=%s", config.vault_dir)

    try:
        import threading
        threading.Event().wait()   # block indefinitely; dispatcher/heartbeat are daemon threads
    except KeyboardInterrupt:
        logger.info("[Worker] Interrupt received — shutting down.")
        sup.shutdown()


def run_sqlite_supervisor_worker() -> None:
    """Run the Supervisor with the SQLite durable queue (A.2 canonical path)."""
    db_url = os.environ.get("SYSTEMU_DATABASE_URL", "")
    if not db_url:
        logger.warning(
            "[Worker] SYSTEMU_DATABASE_URL not set — falling back to in-memory queue."
        )
        run_file_worker()
        return

    config = _load_config()

    from sqlalchemy import create_engine
    from systemu.storage.sqlite.vault import SqliteVault
    from systemu.runtime.supervisor import Supervisor
    from systemu.queue.sqlite_priority_queue import SqlitePriorityQueue
    import uuid

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    vault  = SqliteVault(db_url)

    worker_id  = f"proc-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    task_queue = SqlitePriorityQueue(engine, worker_id=worker_id)

    sup = Supervisor(config, vault, task_queue=task_queue)
    sup.start()
    logger.info(
        "[Worker] SQLite-backend Supervisor started — db=%s worker=%s",
        db_url, worker_id,
    )

    try:
        import threading
        threading.Event().wait()
    except KeyboardInterrupt:
        logger.info("[Worker] Interrupt received — shutting down.")
        sup.shutdown()


def run_huey_worker() -> None:
    """Run the Huey consumer for whichever broker is configured.

    SYSTEMU_QUEUE_BROKER selects SqliteHuey (default) or RedisHuey.  For the
    Redis broker SYSTEMU_DATABASE_URL is *not* required — only SYSTEMU_REDIS_URL.
    For the SQLite broker SYSTEMU_DATABASE_URL must point at the shared DB file.
    """
    try:
        from systemu.queue.huey_task_queue import HueyTaskQueue
        from systemu.queue.huey_app import get_execute_shadow_task
        get_execute_shadow_task()   # force task registration before consumer starts
    except ImportError as exc:
        logger.warning(
            "[Worker] HueyTaskQueue not available (%s). "
            "Falling back to file-backend Supervisor.",
            exc,
        )
        run_file_worker()
        return

    broker = os.environ.get("SYSTEMU_QUEUE_BROKER", "sqlite").lower()

    if broker == "redis":
        redis_url = os.environ.get("SYSTEMU_REDIS_URL", "")
        if not redis_url:
            logger.error(
                "[Worker] SYSTEMU_QUEUE_BROKER=redis but SYSTEMU_REDIS_URL is unset."
            )
            sys.exit(1)
        queue = HueyTaskQueue.create_redis(redis_url)
        broker_descr = f"redis={redis_url}"
    else:
        db_url = os.environ.get("SYSTEMU_DATABASE_URL", "")
        if not db_url:
            logger.error(
                "[Worker] SYSTEMU_DATABASE_URL not set — cannot start Huey SQLite worker."
            )
            sys.exit(1)
        queue = HueyTaskQueue.create_sqlite(db_url)
        broker_descr = f"sqlite={db_url}"

    worker_count = int(os.environ.get("HUEY_WORKERS", "4"))
    logger.info(
        "[Worker] Starting Huey consumer — %s workers=%d", broker_descr, worker_count,
    )

    try:
        from huey.consumer import Consumer
        # Huey's Consumer.__init__ does NOT take `loglevel` — it logs through
        # `huey.consumer` which inherits the root logger's level (set by
        # logging.basicConfig() above).  Don't pass loglevel here; it raises
        # TypeError on every Huey since at least 2.x.
        consumer = Consumer(
            queue.huey,
            workers=worker_count,
            worker_type="thread",
            backoff=1.15,
            max_delay=10.0,
        )
        consumer.run()
    except KeyboardInterrupt:
        logger.info("[Worker] Interrupt received — stopping consumer.")
    except Exception as exc:
        logger.error("[Worker] Huey consumer crashed: %s", exc, exc_info=True)
        sys.exit(1)


# Back-compat alias: older callers import run_sqlite_worker directly.
run_sqlite_worker = run_huey_worker


def main() -> None:
    mode = os.environ.get("SYSTEMU_STORAGE", "file").lower()
    queue = os.environ.get("SYSTEMU_QUEUE", "").lower()
    broker = os.environ.get("SYSTEMU_QUEUE_BROKER", "").lower()
    logger.info(
        "[Worker] Starting — SYSTEMU_STORAGE=%s SYSTEMU_QUEUE=%s SYSTEMU_QUEUE_BROKER=%s",
        mode, queue or "(default)", broker or "(default)",
    )

    # / v0.3.5 — Verify we share the daemon's Python interpreter.
    # In strict mode (SYSTEMU_STRICT_INTERPRETER=1) a mismatch exits the
    # worker before it processes any task; otherwise we log loudly and
    # continue, matching the existing fail-open posture.
    try:
        from pathlib import Path as _Path
        from systemu.runtime.interpreter_check import assert_or_fail
        assert_or_fail(_Path("data"), recorded_by="worker")
    except SystemExit:
        raise
    except Exception:
        logger.debug("[Worker] interpreter check skipped", exc_info=True)

    if queue == "huey":
        logger.info("[Worker] Routing to Huey consumer (broker=%s)", broker or "sqlite")
        run_huey_worker()
    elif mode == "file":
        run_file_worker()
    elif mode in ("sqlite", "postgres"):
        # Canonical path: Supervisor with SQLite durable queue
        run_sqlite_supervisor_worker()
    else:
        logger.warning("[Worker] Unknown storage mode %r — running file worker", mode)
        run_file_worker()


if __name__ == "__main__":
    main()
