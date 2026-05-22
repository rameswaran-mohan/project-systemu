"""E2E: Huey consumer over both brokers in immediate mode.

Verifies the Huey path doesn't have schema/import drift across SqliteHuey and
RedisHuey, and confirms that ``huey_app._reset_for_tests()`` actually clears the
singleton so the second broker gets a fresh instance.
"""

from __future__ import annotations

import pytest


def test_sqlite_huey_immediate_returns_huey_instance(tmp_path, reset_singletons, monkeypatch):
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")
    monkeypatch.delenv("SYSTEMU_QUEUE_BROKER", raising=False)
    monkeypatch.setenv("SYSTEMU_DATABASE_URL", f"sqlite:///{(tmp_path / 'q.db').as_posix()}")

    from systemu.queue.huey_app import get_huey, _reset_for_tests
    _reset_for_tests()
    huey = get_huey()
    # Identity check: SqliteHuey is the concrete type for this branch.
    assert "SqliteHuey" in type(huey).__name__


def test_redis_huey_immediate_returns_redis_instance(fake_redis, reset_singletons, monkeypatch):
    pytest.importorskip("redis")
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")
    monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
    monkeypatch.setenv("SYSTEMU_REDIS_URL", "redis://localhost:6379/0")

    from systemu.queue.huey_app import get_huey, _reset_for_tests
    _reset_for_tests()
    huey = get_huey()
    assert "RedisHuey" in type(huey).__name__


def test_reset_for_tests_clears_singleton_between_brokers(
    fake_redis, tmp_path, reset_singletons, monkeypatch,
):
    """Without _reset_for_tests, the first broker would pin forever."""
    pytest.importorskip("redis")
    monkeypatch.setenv("HUEY_IMMEDIATE", "1")

    from systemu.queue.huey_app import get_huey, _reset_for_tests

    monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "sqlite")
    monkeypatch.setenv("SYSTEMU_DATABASE_URL", f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    _reset_for_tests()
    first = get_huey()
    assert "Sqlite" in type(first).__name__

    monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
    monkeypatch.setenv("SYSTEMU_REDIS_URL", "redis://localhost:6379/0")
    _reset_for_tests()
    second = get_huey()
    assert "Redis" in type(second).__name__
    assert second is not first


def test_redis_broker_without_url_raises(reset_singletons, monkeypatch):
    monkeypatch.setenv("SYSTEMU_QUEUE_BROKER", "redis")
    monkeypatch.delenv("SYSTEMU_REDIS_URL", raising=False)

    from systemu.queue.huey_app import get_huey, _reset_for_tests
    _reset_for_tests()
    with pytest.raises(RuntimeError, match="SYSTEMU_REDIS_URL"):
        get_huey()
