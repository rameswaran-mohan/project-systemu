"""— `memory_dir` defaults to `$SYSTEMU_VAULT_DIR/memory` for Postgres URLs.

Before v0.6.6, every non-SQLite database URL fell through to
`/tmp/systemu_memory`.  In docker modes that path lives in the container's
writable layer — NOT a mounted volume — so every container rebuild lost
the elder + per-shadow memory dirs.  See `captures/E2E_VERDICT_DOCKER.md`
finding D.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from systemu.storage.sqlite.vault import _resolve_memory_dir


class TestResolveMemoryDir:
    def test_postgres_url_uses_vault_dir_memory(self, monkeypatch, tmp_path):
        vault_dir = tmp_path / "vault"
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault_dir))
        result = _resolve_memory_dir("postgresql://u:p@h:5432/db", None)
        assert result == vault_dir / "memory"

    def test_postgres_url_short_scheme_also_works(self, monkeypatch, tmp_path):
        vault_dir = tmp_path / "vault2"
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(vault_dir))
        result = _resolve_memory_dir("postgres://u:p@h:5432/db", None)
        assert result == vault_dir / "memory"

    def test_postgres_url_default_vault_dir(self, monkeypatch):
        monkeypatch.delenv("SYSTEMU_VAULT_DIR", raising=False)
        result = _resolve_memory_dir("postgresql://u:p@h:5432/db", None)
        # Default falls back to /data/vault (the docker-image bind point)
        assert result == Path("/data/vault") / "memory"

    def test_sqlite_url_uses_db_parent_memory(self, tmp_path):
        db_path = tmp_path / "data" / "systemu.db"
        result = _resolve_memory_dir(f"sqlite:///{db_path.as_posix()}", None)
        assert result == db_path.parent / "memory"

    def test_explicit_memory_dir_overrides_default(self, monkeypatch, tmp_path):
        # Postgres URL would normally yield $SYSTEMU_VAULT_DIR/memory, but an
        # explicit memory_dir overrides any URL-derived path.
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", "/should-not-be-used")
        custom = tmp_path / "custom_memory"
        result = _resolve_memory_dir("postgresql://u:p@h/db", custom)
        assert result == custom

    def test_unknown_scheme_falls_back_to_tmp_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="systemu.storage.sqlite.vault"):
            result = _resolve_memory_dir("mysql://foo/bar", None)
        assert result == Path("/tmp/systemu_memory")
        assert any(
            "memory_dir" in r.message and "unrecognized" in r.message
            for r in caplog.records
        )

    def test_sqlite_url_does_not_emit_warning(self, caplog, tmp_path):
        with caplog.at_level(logging.WARNING, logger="systemu.storage.sqlite.vault"):
            _resolve_memory_dir(f"sqlite:///{(tmp_path / 'x.db').as_posix()}", None)
        # No warnings about memory_dir for legit SQLite URLs
        assert not any("memory_dir" in r.message for r in caplog.records)

    def test_postgres_url_does_not_emit_warning(self, caplog, monkeypatch, tmp_path):
        monkeypatch.setenv("SYSTEMU_VAULT_DIR", str(tmp_path / "vault"))
        with caplog.at_level(logging.WARNING, logger="systemu.storage.sqlite.vault"):
            _resolve_memory_dir("postgresql://u:p@h/db", None)
        # Postgres URLs no longer warn — they have a proper default
        assert not any("memory_dir" in r.message for r in caplog.records)
