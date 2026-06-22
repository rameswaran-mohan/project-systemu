"""Huey application instance — shared by producer (dashboard) and consumer (worker).

The Huey instance is created LAZILY on first call to get_huey() rather than at
module import time.  This prevents the critical ordering bug where any import of
this module before SYSTEMU_DATABASE_URL is set would create a Huey instance
pointing at the wrong DB file.

Consumer entrypoint (run by systemu-worker service):
    huey_consumer systemu.queue.huey_app:get_huey() -w 4 -k thread
    # Or via the worker module:
    python -m systemu.worker

Shadow execution tasks are defined below and automatically discovered by the
consumer when it imports this module (which triggers get_huey() on decoration).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
#  Lazy Huey singleton
# ─────────────────────────────────────────────────────────────────────────────

_huey_lock     = threading.Lock()
_huey_instance: Optional[Any] = None   # SqliteHuey once initialised


def _resolve_db_path() -> str:
    """Derive the SQLite DB filename from SYSTEMU_DATABASE_URL."""
    raw = os.environ.get("SYSTEMU_DATABASE_URL", "")
    if raw.startswith("sqlite:////"):
        return "/" + raw[len("sqlite:////"):]
    if raw.startswith("sqlite:///"):
        return raw[len("sqlite:///"):]
    # Not set or not a SQLite URL — use a sensible default alongside data/
    return str(Path.cwd() / "data" / "systemu.db")


def _build_sentinel_pool(url: str) -> Any:
    """Construct a SentinelConnectionPool from a redis+sentinel:// URL.

    URL form:
        redis+sentinel://host1:26379,host2:26379,host3:26379/<service>/<db>?password=...

    Where:
        service  — Sentinel-monitored service name (e.g. ``mymaster``).
        db       — optional DB index (default 0).
        password — optional ``?password=…`` for both sentinels and master.

    Returns a redis.sentinel.SentinelConnectionPool that redis-py / huey can
    use directly — failovers are handled transparently.
    """
    from urllib.parse import urlsplit, parse_qs
    from redis.sentinel import Sentinel, SentinelConnectionPool

    parsed = urlsplit(url)
    hosts: List[tuple] = []
    for hp in parsed.netloc.split(","):
        if not hp:
            continue
        host, _, port = hp.partition(":")
        hosts.append((host, int(port or 26379)))

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError(
            "Sentinel URL must include the service name: "
            "redis+sentinel://host:port/<service>"
        )
    service_name = parts[0]
    db = int(parts[1]) if len(parts) > 1 else 0
    qs = parse_qs(parsed.query)
    password = (qs.get("password") or [None])[0]

    sentinel = Sentinel(
        hosts,
        password=password,
        socket_timeout=2.0,
    )
    return SentinelConnectionPool(
        service_name, sentinel,
        db=db, password=password,
    )


def _mask_url(url: str) -> str:
    """Return *url* with any embedded password masked — for log lines.  Avoids
    leaking the redis password into stdout when get_huey() logs the topology."""
    from urllib.parse import urlsplit, urlunsplit
    try:
        p = urlsplit(url)
        if p.password:
            netloc = f"{p.username or ''}:***@{p.hostname}"
            if p.port:
                netloc += f":{p.port}"
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:
        pass
    return url


def _reset_for_tests() -> None:
    """Drop the cached singleton so the next ``get_huey()`` re-reads the env.

    Tests that exercise both broker types in the same process need this — the
    module-level cache otherwise pins the first broker forever.  Production
    callers must not invoke this; the singleton invariant is what makes the
    Huey consumer and the dashboard share a registry.
    """
    global _huey_instance
    with _huey_lock:
        _huey_instance = None


def get_huey(db_path: Optional[str] = None) -> Any:
    """Return the shared Huey singleton, creating it on first call.

    The broker is selected by ``SYSTEMU_QUEUE_BROKER``:
        sqlite (default) → SqliteHuey at SYSTEMU_DATABASE_URL or db_path
        redis            → RedisHuey at SYSTEMU_REDIS_URL

    Args:
        db_path: Optional explicit .db file path (sqlite broker only).  When
                 omitted the path is derived from SYSTEMU_DATABASE_URL.  The
                 first call wins — subsequent calls ignore db_path and return
                 the cached instance.  Ignored entirely when broker=redis.

    Raises:
        ImportError: when huey (or redis-py for the redis broker) is missing.
        RuntimeError: when broker=redis but SYSTEMU_REDIS_URL is unset.
    """
    global _huey_instance
    if _huey_instance is not None:
        return _huey_instance

    with _huey_lock:
        # Double-checked locking — another thread may have initialised while
        # we were waiting on the lock.
        if _huey_instance is not None:
            return _huey_instance

        # immediate=True makes tasks execute synchronously — set HUEY_IMMEDIATE=1
        # in tests to avoid needing a running worker process.
        _immediate = os.environ.get("HUEY_IMMEDIATE", "").lower() in ("1", "true", "yes")
        broker = os.environ.get("SYSTEMU_QUEUE_BROKER", "sqlite").lower()

        if broker == "redis":
            redis_url = os.environ.get("SYSTEMU_REDIS_URL", "")
            if not redis_url:
                raise RuntimeError(
                    "SYSTEMU_QUEUE_BROKER=redis but SYSTEMU_REDIS_URL is not set."
                )
            try:
                from huey import RedisHuey as _RedisHuey
                import redis as _redis_client
            except ImportError as exc:
                raise ImportError(
                    "Huey + redis are not installed. Run: "
                    "pip install -e '.[docker-enterprise]'"
                ) from exc

            # Topology selection — see docs/redis-topologies.md for the matrix:
            #   redis://   → standalone or compose-network
            #   rediss://  → TLS-wrapped standalone (managed Redis,
            #                Elasticache-with-encryption, Upstash, etc.)
            #   redis+sentinel://host1:26379,host2:26379/<service>/<db>
            #               → Sentinel-fronted HA cluster
            redis_kwargs: Dict[str, Any] = {
                "store_none": True,
                "immediate": _immediate,
            }
            if redis_url.startswith("redis+sentinel://"):
                redis_kwargs["connection_pool"] = _build_sentinel_pool(redis_url)
                _huey_instance = _RedisHuey("systemu", **redis_kwargs)
                topology_descr = "sentinel"
            else:
                # rediss:// (TLS) vs redis:// (plain) — both pass through
                # straight to redis-py via huey's url= param; redis-py picks
                # the right connection class from the scheme.
                redis_kwargs["url"] = redis_url
                _huey_instance = _RedisHuey("systemu", **redis_kwargs)
                topology_descr = "tls" if redis_url.startswith("rediss://") else "standalone"
            logger.info(
                "[HueyApp] RedisHuey initialised — topology=%s url=%s immediate=%s",
                topology_descr, _mask_url(redis_url), _immediate,
            )
            return _huey_instance

        # ── sqlite broker (default) ──────────────────────────────────────────
        try:
            from huey import SqliteHuey as _SqliteHuey
        except ImportError as exc:
            raise ImportError(
                "Huey is not installed. Run: pip install 'huey>=2.5' "
                "or pip install -e '.[local]'"
            ) from exc

        resolved = db_path or _resolve_db_path()
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)

        _huey_instance = _SqliteHuey(
            "systemu",
            filename=resolved,
            store_none=True,
            immediate=_immediate,
        )
        logger.info(
            "[HueyApp] SqliteHuey initialised — db=%s immediate=%s", resolved, _immediate
        )
        return _huey_instance


def reset_huey() -> None:
    """Reset the Huey singleton — for testing only."""
    global _huey_instance
    with _huey_lock:
        _huey_instance = None


# ─────────────────────────────────────────────────────────────────────────────
#  Convenience alias — ``huey`` attribute expected by huey_consumer CLI
#
#  The CLI runs:  huey_consumer systemu.queue.huey_app.huey
#  That works because ``huey`` is a module-level name.  We make it a lazy
#  property by using a class with __get__, but since the module isn't a class,
#  we just initialise it lazily on first attribute access via a module-level
#  __getattr__ (Python 3.7+).
# ─────────────────────────────────────────────────────────────────────────────

def __getattr__(name: str) -> Any:
    """Lazy module-level attribute — ``import systemu.queue.huey_app; huey_app.huey``
    triggers get_huey() on first access rather than at import time.
    """
    if name == "huey":
        return get_huey()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ─────────────────────────────────────────────────────────────────────────────
#  Shadow execution task
# ─────────────────────────────────────────────────────────────────────────────

def _make_execute_shadow_task(huey_instance: Any):  # type: ignore[return]
    """Register and return the execute_shadow Huey task on the given huey instance."""

    @huey_instance.task(name="execute_shadow")
    def execute_shadow_task(
        activity_id: str,
        shadow_id: str,
        priority: int = 5,
        reason: str = "manual",
        retry_count: int = 0,
    ) -> Dict[str, Any]:
        """Execute a Shadow activity — runs in the Huey worker process.

        This task is self-contained: it bootstraps its own AppState using
        SYSTEMU_STORAGE and SYSTEMU_DATABASE_URL from the worker environment,
        then runs the shadow execution loop and returns the result dict.

        The result is stored in Huey's result store (same SQLite DB) and can
        be retrieved by the dashboard via result.get(timeout=...).
        """
        task_logger = logging.getLogger("systemu.queue.huey_tasks")
        task_logger.info(
            "[HueyTask] execute_shadow — activity=%s shadow=%s retry=%d reason=%s",
            activity_id, shadow_id, retry_count, reason,
        )

        try:
            import os
            from sharing_on.config import Config
            from systemu.interface.dashboard_state import AppState

            # v0.6.8-g: read .env / process env so SYSTEMU_TIER{1,2}_MODEL
            # actually reaches the worker.  Bare Config() picked dataclass
            # defaults and ignored the operator's tier-model overrides.
            config = Config.from_env()

            # Honour explicit vault dir override from environment
            vault_dir_env = os.environ.get("SYSTEMU_VAULT_DIR")
            if vault_dir_env and hasattr(config, "vault_dir"):
                config.vault_dir = vault_dir_env

            state = AppState.create(config)

            shadow   = state.vault.get_shadow(shadow_id)
            activity = state.vault.get_activity(activity_id)

            task_logger.info(
                "[HueyTask] Running shadow '%s' on activity '%s'",
                shadow.name, activity.name,
            )

            # Run async execute() in a fresh event loop (Huey threads have none)
            from systemu.runtime.shadow_runtime import ShadowRuntime
            runtime = ShadowRuntime(config, state.vault)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    runtime.execute(shadow, activity)
                )
            finally:
                loop.close()
                asyncio.set_event_loop(None)

            task_logger.info(
                "[HueyTask] execute_shadow complete — status=%s", result.get("status")
            )
            return result

        except Exception as exc:
            task_logger.error(
                "[HueyTask] execute_shadow FAILED — %s", exc, exc_info=True
            )
            return {
                "status":        "failure",
                "error":         str(exc),
                "final_summary": f"Worker task error: {exc}",
                "activity_id":   activity_id,
                "shadow_id":     shadow_id,
            }

    return execute_shadow_task


# Cache so the task is only registered once per Huey instance
_execute_shadow_task: Optional[Any] = None


def get_execute_shadow_task() -> Any:
    """Return the execute_shadow Huey task, registering it if necessary."""
    global _execute_shadow_task
    if _execute_shadow_task is None:
        h = get_huey()
        _execute_shadow_task = _make_execute_shadow_task(h)
    return _execute_shadow_task


# Stub for bare ``from systemu.queue.huey_app import execute_shadow_task``
# imports (used by worker.py to force task registration).
def __dir__():
    return ["get_huey", "get_execute_shadow_task", "reset_huey", "huey", "execute_shadow_task"]
