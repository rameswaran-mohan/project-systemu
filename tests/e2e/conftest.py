"""Shared fixtures for e2e tests.

The e2e suite intentionally exercises Supervisor + queue + (sometimes) Huey
end-to-end without launching real services.  These fixtures factor out the
boilerplate: a SQLite engine with the supervisor_queue schema, a Vault on
tmp_path, and a Supervisor singleton reset between tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import pytest


@pytest.fixture
def reset_singletons():
    """Drop module-level singletons so each test starts clean.

    Exposes a context manager-like fixture that yields once, then tears down
    Supervisor + huey_app caches.  Necessary because both classes deliberately
    persist a process-wide singleton.
    """
    yield
    # Tear down Supervisor singleton
    try:
        from systemu.runtime.supervisor import Supervisor
        if Supervisor._instance is not None:
            try:
                Supervisor._instance.shutdown()
            except Exception:
                pass
            Supervisor._instance = None
    except Exception:
        pass

    # Tear down huey_app cache
    try:
        from systemu.queue import huey_app
        huey_app._reset_for_tests()
    except Exception:
        pass

    # Tear down EventBus subscribers
    try:
        from systemu.interface.event_bus import EventBus
        bus = EventBus.get()
        with bus._sub_lock:
            bus._subscribers.clear()
            bus._buffer.clear()
    except Exception:
        pass


@pytest.fixture
def clean_env(monkeypatch):
    """Strip SYSTEMU_* env vars so tests get a known baseline.  Tests that
    need specific values set them via monkeypatch.setenv after this fixture."""
    for key in list(os.environ):
        if key.startswith("SYSTEMU_") or key.startswith("HUEY_"):
            monkeypatch.delenv(key, raising=False)
    yield monkeypatch


@pytest.fixture
def sqlite_engine(tmp_path):
    """A fresh SQLite engine with the supervisor_queue table created."""
    from sqlalchemy import create_engine, text
    db_path = tmp_path / "queue.db"
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    # Reuse the production schema definition rather than duplicating it here.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS supervisor_queue (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id   TEXT UNIQUE NOT NULL,
                activity_id     TEXT NOT NULL,
                shadow_id       TEXT NOT NULL,
                priority        INTEGER NOT NULL DEFAULT 5,
                retry_count     INTEGER NOT NULL DEFAULT 0,
                reason          TEXT,
                enqueued_at     REAL NOT NULL,
                state           TEXT NOT NULL DEFAULT 'queued',
                attempt_count   INTEGER NOT NULL DEFAULT 0,
                claimed_by      TEXT,
                claimed_at      REAL,
                last_heartbeat_at REAL,
                result_json     TEXT,
                error_text      TEXT
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_q_state_prio "
            "ON supervisor_queue(state, priority, enqueued_at)"
        ))
    return engine


@pytest.fixture
def fake_redis(monkeypatch):
    """Replace redis.Redis.from_url with a fakeredis-backed instance."""
    fakeredis = pytest.importorskip("fakeredis")
    server = fakeredis.FakeServer()

    class _FakeRedis(fakeredis.FakeStrictRedis):
        @classmethod
        def from_url(cls, url, decode_responses=False, **kw):
            return cls(server=server, decode_responses=decode_responses)

    import redis as real_redis
    monkeypatch.setattr(real_redis, "Redis", _FakeRedis)
    return server


@pytest.fixture
def minimal_vault(tmp_path):
    """Vault with the bare directory layout the runtime expects."""
    subs: Iterable[str] = (
        "scrolls", "activities", "shadow_army", "skills",
        "tools/implementations", "evolutions", "notifications", "executions",
    )
    for sub in subs:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    for idx_dir in ("scrolls", "activities", "shadow_army", "skills", "tools", "evolutions"):
        (tmp_path / idx_dir / "index.json").write_text("[]", encoding="utf-8")
    (tmp_path / "global_memory.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "chat_history.jsonl").write_text("", encoding="utf-8")
    from systemu.vault.vault import Vault
    return Vault(str(tmp_path))


@pytest.fixture
def real_config(tmp_path):
    """Real Config dataclass populated for use in e2e tests."""
    from sharing_on.config import Config
    cfg = Config()
    cfg.vault_dir = str(tmp_path)
    cfg.output_dir = str(tmp_path / "outputs")
    return cfg
