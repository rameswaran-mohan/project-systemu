"""open_vault — single authoritative factory for the active vault backend.

Every component (CLI, daemon scheduler, AppState) calls this to obtain a
vault instance so they all read/write the same storage backend.

Backend selection (SYSTEMU_STORAGE env var):
  "file"     (default) — JSON-file vault at config.vault_dir
  "sqlite"             — SQLAlchemy SQLite vault at data/systemu.db
                         (SYSTEMU_DATABASE_URL overrides the path)
  "postgres"           — SQLAlchemy PostgreSQL vault (DATABASE_URL required)
  "parallel"           — dual-write: file (primary) + sqlite (secondary)

If the requested backend cannot be imported or initialised, falls back to
the file vault with a WARNING log entry so the system stays usable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sharing_on.config import Config

logger = logging.getLogger(__name__)


def open_vault(config: "Config"):
    """Return the vault backend determined by SYSTEMU_STORAGE.

    The returned object implements the same interface as Vault (save_scroll,
    get_scroll, list_scrolls, …) so callers are storage-agnostic.
    """
    mode = os.environ.get("SYSTEMU_STORAGE", "file").lower()

    if mode == "sqlite":
        return _open_sqlite(config)
    elif mode == "postgres":
        return _open_postgres(config)
    elif mode == "parallel":
        return _open_parallel(config)
    else:
        if mode != "file":
            logger.warning(
                "[VaultFactory] Unknown SYSTEMU_STORAGE=%r — falling back to 'file'", mode
            )
        return _open_file(config)


# ── Backends ──────────────────────────────────────────────────────────────────

def _open_file(config: "Config"):
    from systemu.vault.vault import Vault
    return Vault(config.vault_dir)


def _open_sqlite(config: "Config"):
    try:
        from systemu.storage.sqlite.vault import SqliteVault
        db_url = _sqlite_url(config)
        _ensure_sqlite_dir(db_url)
        return SqliteVault(db_url)
    except ImportError as exc:
        logger.warning(
            "[VaultFactory] SQLite backend unavailable (%s) — falling back to file", exc
        )
        return _open_file(config)
    except Exception as exc:
        logger.warning(
            "[VaultFactory] SQLite vault init failed (%s) — falling back to file", exc
        )
        return _open_file(config)


def _open_postgres(config: "Config"):
    # Accept either SYSTEMU_DATABASE_URL (preferred — matches the rest of
    # the codebase + docker-compose.yml + install.py) or the legacy
    # bare DATABASE_URL.  We previously read only DATABASE_URL, which made
    # every docker-* deployment silently fall back to the file backend
    # because compose passes SYSTEMU_DATABASE_URL.
    database_url = (
        os.environ.get("SYSTEMU_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not database_url:
        logger.warning(
            "[VaultFactory] postgres mode requires SYSTEMU_DATABASE_URL "
            "(or DATABASE_URL) — falling back to file"
        )
        return _open_file(config)
    try:
        from systemu.storage.sqlite.vault import SqliteVault  # SA handles postgresql://
        return SqliteVault(database_url)
    except Exception as exc:
        logger.warning("[VaultFactory] PostgreSQL vault init failed (%s) — falling back to file", exc)
        return _open_file(config)


def _open_parallel(config: "Config"):
    try:
        from systemu.vault.vault import Vault as _RawVault
        from systemu.storage.file_vault import FileVault
        from systemu.storage.sqlite.vault import SqliteVault
        from systemu.storage.parallel_vault import ParallelVault

        db_url = _sqlite_url(config)
        _ensure_sqlite_dir(db_url)
        primary   = FileVault(_RawVault(config.vault_dir))
        secondary = SqliteVault(db_url)
        return ParallelVault(primary, secondary)
    except Exception as exc:
        logger.warning("[VaultFactory] Parallel vault init failed (%s) — falling back to file", exc)
        return _open_file(config)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sqlite_url(config: "Config") -> str:
    return os.environ.get(
        "SYSTEMU_DATABASE_URL",
        f"sqlite:///{Path(config.vault_dir).parent / 'data' / 'systemu.db'}",
    )


def _ensure_sqlite_dir(db_url: str) -> None:
    """Create the parent directory for file-based SQLite URLs if needed."""
    if db_url.startswith("sqlite:////"):
        Path("/" + db_url[len("sqlite:////"):]).parent.mkdir(parents=True, exist_ok=True)
    elif db_url.startswith("sqlite:///"):
        Path(db_url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
