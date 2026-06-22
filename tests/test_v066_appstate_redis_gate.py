"""v0.6.6-c — `_create_postgres_backend` requires Redis only when broker=redis.

Before v0.6.6 the dashboard's AppState rejected the docker-local combo of
(storage=postgres, queue_broker=sqlite, no Redis) and silently fell back to
FileVault — while the worker continued writing to Postgres.  Split-brain.

See ``captures/E2E_VERDICT_DOCKER.md`` finding A.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# A sentinel returned by our patched ``_create_file_backend`` so tests can
# distinguish "fell back to file" from "successfully wired postgres".
_FILE_FALLBACK = object()


@pytest.fixture
def stub_postgres_wiring(monkeypatch):
    """Stub out the heavy postgres-backend wiring so we test only the gate.

    Patches:
      * _create_file_backend → returns ``_FILE_FALLBACK`` sentinel
      * SqliteVault, MemoryEventBroker, NotificationApprovalGate, Supervisor.init,
        ThreadTaskQueue, EventBus.get, AppState() constructor → MagicMock
    """
    import systemu.interface.dashboard_state as ds
    monkeypatch.setattr(
        ds.AppState, "_create_file_backend",
        classmethod(lambda cls, cfg: _FILE_FALLBACK),
    )

    # Patch the dynamic imports inside the try-block so the postgres
    # success path returns a sentinel-distinct value (a MagicMock cast as
    # an AppState).  Each of these is imported INSIDE the function so we
    # patch the import target, not the module's top-level binding.
    monkeypatch.setattr(
        "systemu.storage.sqlite.vault.SqliteVault",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "systemu.events.memory_event_broker.MemoryEventBroker",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "systemu.approval.notification_gate.NotificationApprovalGate",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "systemu.runtime.supervisor.Supervisor.init",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "systemu.queue.thread_task_queue.ThreadTaskQueue",
        MagicMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "systemu.interface.event_bus.EventBus.get",
        classmethod(lambda cls: MagicMock()),
    )
    # AppState.__init__ does real work — stub it to a no-op so the success
    # path returns a constructed instance without side-effects.
    monkeypatch.setattr(
        ds.AppState, "__init__",
        lambda self, *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "systemu.interface.notifications.set_vault",
        lambda v: None,
    )

    return ds


def _clear_env(monkeypatch):
    for k in (
        "SYSTEMU_DATABASE_URL", "DATABASE_URL",
        "SYSTEMU_REDIS_URL", "REDIS_URL",
        "SYSTEMU_QUEUE_BROKER",
    ):
        monkeypatch.delenv(k, raising=False)


class TestPostgresWithSqliteBroker:
    """docker-local: postgres + Huey-SQLite → Redis NOT required."""

    def test_passes_without_redis(self, stub_postgres_wiring, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("SYSTEMU_DATABASE_URL", "postgresql://u:p@h:5432/d")
        monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "sqlite")

        ds = stub_postgres_wiring
        result = ds.AppState._create_postgres_backend(MagicMock())
        assert result is not _FILE_FALLBACK, (
            "docker-local (queue_broker=sqlite) must NOT fall back to file"
        )

    def test_passes_when_broker_unset(self, stub_postgres_wiring, monkeypatch):
        """Defaults to 'sqlite' broker when env unset."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("SYSTEMU_DATABASE_URL", "postgresql://u:p@h:5432/d")
        # SYSTEMU_QUEUE_BROKER intentionally unset

        ds = stub_postgres_wiring
        result = ds.AppState._create_postgres_backend(MagicMock())
        assert result is not _FILE_FALLBACK


class TestPostgresWithRedisBroker:
    """docker-enterprise: postgres + Huey-Redis → Redis IS required."""

    def test_passes_with_both_urls(self, stub_postgres_wiring, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("SYSTEMU_DATABASE_URL", "postgresql://u:p@h:5432/d")
        monkeypatch.setenv("SYSTEMU_REDIS_URL", "redis://r:6379/0")
        monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")

        ds = stub_postgres_wiring
        result = ds.AppState._create_postgres_backend(MagicMock())
        assert result is not _FILE_FALLBACK

    def test_falls_back_when_redis_missing(self, stub_postgres_wiring, monkeypatch):
        _clear_env(monkeypatch)
        monkeypatch.setenv("SYSTEMU_DATABASE_URL", "postgresql://u:p@h:5432/d")
        monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
        # SYSTEMU_REDIS_URL intentionally unset — enterprise without Redis

        ds = stub_postgres_wiring
        result = ds.AppState._create_postgres_backend(MagicMock())
        assert result is _FILE_FALLBACK, (
            "docker-enterprise without Redis URL must fall back to file"
        )


class TestMissingDatabase:
    """No DB URL → always falls back, regardless of broker."""

    def test_missing_db_with_sqlite_broker_falls_back(
        self, stub_postgres_wiring, monkeypatch,
    ):
        _clear_env(monkeypatch)
        monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "sqlite")

        ds = stub_postgres_wiring
        result = ds.AppState._create_postgres_backend(MagicMock())
        assert result is _FILE_FALLBACK

    def test_missing_db_with_redis_broker_falls_back(
        self, stub_postgres_wiring, monkeypatch,
    ):
        _clear_env(monkeypatch)
        monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
        monkeypatch.setenv("SYSTEMU_REDIS_URL", "redis://r:6379/0")

        ds = stub_postgres_wiring
        result = ds.AppState._create_postgres_backend(MagicMock())
        assert result is _FILE_FALLBACK
