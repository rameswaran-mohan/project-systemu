"""v0.9.7 — psycopg2 graceful-degrade tests (Phase 4.3a latent-bug fix).

Background: a web/action-audit execution path could crash with
``ModuleNotFoundError: No module named 'psycopg2'`` because the Postgres vault
backend imported psycopg2 with no fallback.  The fix:

  1. ``postgres_backend`` imports psycopg2 defensively (``psycopg2 = None`` when
     absent), so importing the module never raises.  Functions that need the
     driver raise a clear, catchable ``RuntimeError("psycopg2 not available")``.
  2. The ``systemu.vault.backend`` dispatch layer detects psycopg2
     unavailability and degrades a ``postgres`` vault to the ``sqlite`` backend,
     logging a single warning, instead of crashing the caller.

These tests force psycopg2 absent (via the module-level ``psycopg2`` sentinel)
and assert the import-safety + graceful fallback.  They also confirm the
postgres path is still selected when psycopg2 IS available (asserted with a
truthy sentinel so the suite runs without a real psycopg2 install).
"""
import importlib
import sys
from pathlib import Path

import pytest

from systemu.vault.vault import Vault


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_postgres_vault(tmp_path: Path) -> Vault:
    """A Vault wired for the postgres backend, but with a sqlite db ready as the
    degrade target (so the fallback path can actually write/read)."""
    v = Vault(root=tmp_path)
    v._storage_backend = "postgres"
    v._postgres_url = "postgresql://unused@localhost/unused"
    # Provide the sqlite degrade target up front, schema-initialised.
    v._sqlite_url = f"sqlite:///{tmp_path}/vault.db"
    from systemu.vault.backend.sqlite_backend import ensure_schema
    ensure_schema(v)
    return v


# ---------------------------------------------------------------------------
# 1. Import-safety: postgres_backend imports cleanly when psycopg2 is absent
# ---------------------------------------------------------------------------

class TestImportSafe:
    def test_import_with_psycopg2_absent_does_not_raise(self, monkeypatch):
        """Forcing sys.modules['psycopg2'] = None makes `import psycopg2` raise
        ImportError; a fresh import of postgres_backend must still succeed and
        expose ``psycopg2 is None`` rather than crashing at import time."""
        # Force the absent state and drop any cached copy of the backend module.
        monkeypatch.setitem(sys.modules, "psycopg2", None)
        monkeypatch.delitem(sys.modules,
                            "systemu.vault.backend.postgres_backend",
                            raising=False)

        pg = importlib.import_module("systemu.vault.backend.postgres_backend")
        # Import succeeded (no exception) and the sentinel reflects absence.
        assert pg.psycopg2 is None

        # The functions that need the driver raise a clear, catchable error
        # rather than an uncaught ModuleNotFoundError.
        with pytest.raises(RuntimeError, match="psycopg2 not available"):
            pg._connect(object())

    def test_functions_raise_runtimeerror_when_absent(self, monkeypatch):
        pg = importlib.import_module("systemu.vault.backend.postgres_backend")
        monkeypatch.setattr(pg, "psycopg2", None)
        with pytest.raises(RuntimeError, match="psycopg2 not available"):
            pg.dispatch_append_action_audit(object(), {
                "ts": "t", "execution_id": "e", "objective_id": 1,
                "action": "a", "params": {}, "success": True, "error": None,
            })


# ---------------------------------------------------------------------------
# 2. Dispatch degrades postgres -> sqlite when psycopg2 is unavailable
# ---------------------------------------------------------------------------

class TestDispatchDegrades:
    def _force_absent(self, monkeypatch):
        import systemu.vault.backend.postgres_backend as pg
        monkeypatch.setattr(pg, "psycopg2", None)

    def test_resolve_backend_returns_sqlite(self, monkeypatch, tmp_path):
        self._force_absent(monkeypatch)
        from systemu.vault import backend
        v = _make_postgres_vault(tmp_path)
        assert v._storage_backend == "postgres"
        # Resolution degrades to sqlite without raising.
        assert backend._resolve_backend(v) == "sqlite"

    def test_append_does_not_raise_and_lands_in_sqlite(self, monkeypatch, tmp_path):
        """The core bug: append on a postgres vault with psycopg2 missing must
        NOT crash with ModuleNotFoundError — it degrades to sqlite and the row
        is readable back through the same vault."""
        self._force_absent(monkeypatch)
        v = _make_postgres_vault(tmp_path)
        v.append_action_audit({
            "ts": "2026-06-08T12:00:00Z",
            "user_id": "alice",
            "execution_id": "e1",
            "objective_id": 1,
            "action": "web.fetch",
            "params": {"url": "https://example.com"},
            "success": True,
            "error": None,
        })
        rows = v.query_action_audit(execution_id="e1")
        assert len(rows) == 1
        assert rows[0]["action"] == "web.fetch"
        assert rows[0]["params"] == {"url": "https://example.com"}

    def test_degrade_logs_single_warning(self, monkeypatch, tmp_path, caplog):
        self._force_absent(monkeypatch)
        from systemu.vault import backend
        v = _make_postgres_vault(tmp_path)
        with caplog.at_level("WARNING", logger="systemu.vault.backend"):
            backend._resolve_backend(v)
            backend._resolve_backend(v)  # second call should NOT re-warn
        warnings = [r for r in caplog.records
                    if "psycopg2 not available" in r.getMessage()]
        assert len(warnings) == 1, (
            "fallback should log exactly one warning, not spam every call"
        )


# ---------------------------------------------------------------------------
# 3. Happy path: postgres still selected when psycopg2 IS available
# ---------------------------------------------------------------------------

class TestPostgresSelectedWhenAvailable:
    def test_resolve_backend_keeps_postgres_with_truthy_sentinel(
            self, monkeypatch, tmp_path):
        """When psycopg2 is present (real install or a truthy sentinel), the
        dispatch layer must NOT degrade — it keeps the postgres backend."""
        import systemu.vault.backend.postgres_backend as pg

        class _FakePsycopg2:  # truthy, stands in for a real install
            pass

        monkeypatch.setattr(pg, "psycopg2", _FakePsycopg2())
        from systemu.vault import backend
        v = Vault(root=tmp_path)
        v._storage_backend = "postgres"
        v._postgres_url = "postgresql://unused@localhost/unused"
        assert backend._resolve_backend(v) == "postgres"
        assert backend._postgres_available() is True

    def test_non_postgres_backends_unaffected(self, monkeypatch, tmp_path):
        """sqlite/file vaults resolve to themselves regardless of psycopg2."""
        import systemu.vault.backend.postgres_backend as pg
        monkeypatch.setattr(pg, "psycopg2", None)
        from systemu.vault import backend

        v_sqlite = Vault(root=tmp_path)
        v_sqlite._storage_backend = "sqlite"
        assert backend._resolve_backend(v_sqlite) == "sqlite"

        v_file = Vault(root=tmp_path)
        # No _storage_backend set -> defaults to "file"
        assert backend._resolve_backend(v_file) == "file"
