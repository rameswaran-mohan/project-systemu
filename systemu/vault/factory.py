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


# ── The recovery store (F31) ──────────────────────────────────────────────────

#: The vault surface ``RecoveryEngine`` needs in order to look a record up. The
#: file vault implements none of them (it has ``get_*``, which raises, not the
#: ``find_*`` "None if absent" pair, and no ``skill_exists``).
RECOVERY_VAULT_METHODS = (
    "find_scroll", "find_activity", "find_activity_for_scroll",
    "find_shadow", "find_tool", "skill_exists",
)


#: What to tell the operator when the recovery lookup is NOT served by the
#: active backend. The wording lives here, beside the decision, for two reasons:
#: the mint is the only thing that knows which store won, and a CLI callback that
#: merely NAMED the env var would trip the flat prohibition in
#: tests/test_f2_default_file_storage_cli.py -- correctly, since that fence
#: cannot distinguish a message from a storage read, and teaching it to would
#: reintroduce the decoy-able predicate F31 exists to remove.
RECOVERY_SOURCE_NOTES = {
    "database_url": (
        "note: serving this lookup from SYSTEMU_DATABASE_URL; the active storage "
        "backend cannot answer scoped queries, so diagnoses -- and any --apply "
        "repairs -- act on that store, not the one every other command reads."
    ),
}


def _serves_recovery(vault) -> bool:
    return vault is not None and not [
        m for m in RECOVERY_VAULT_METHODS if not callable(getattr(vault, m, None))
    ]


def open_recovery_vault(config: "Config"):
    """THE ONE MINT for "which store answers a scoped-recovery lookup".

    Returns ``(vault, source)`` where ``source`` is ``"active"``, ``"database_url"``
    or ``None``. A ``None`` source means nothing can serve the lookup, and the
    caller must refuse; it never returns a vault that cannot serve.

    WHY THIS LIVES IN THE FACTORY (F31). It used to live inline in the ``doctor``
    callback, and that put a database-URL read inside a CLI command -- the exact
    shape ``tests/test_f2_default_file_storage_cli.py`` exists to forbid. Fencing
    it there forced the fence to reason about textual call ORDER ("a URL read is
    fine as long as some open_vault call appears above it"), and an ordering rule
    over source text is satisfiable by a decoy: a review demonstrated five
    realistic bad shapes -- a discarded open_vault call, a dead-code decoy, a
    same-line ternary, an attribute-form constructor, an unconditional override
    below the call -- all of which the original strict fence caught and the
    ordering fence let through. Moving the decision here means no CLI callback
    needs to mention a URL at all, so the fence goes back to a flat prohibition,
    which cannot be decoyed (DEC-43: one mint; DEC-34: do not let the checked
    party supply the thing that satisfies the check).

    PRECEDENCE. The ACTIVE backend wins, so a default file-storage install is
    served -- or honestly refused -- exactly as before. ``SYSTEMU_DATABASE_URL``
    is consulted only when the active backend cannot serve, because supplying it
    is how an operator explicitly asks for the SQL recovery store and is the
    documented v0.6.8 contract.
    """
    vault = open_vault(config)
    if _serves_recovery(vault):
        return vault, "active"

    db_url = os.environ.get("SYSTEMU_DATABASE_URL")
    if db_url:
        try:
            from systemu.storage.sqlite.vault import SqliteVault

            candidate = SqliteVault(db_url)
        except Exception as exc:
            logger.warning(
                "[VaultFactory] SYSTEMU_DATABASE_URL is set but that store could "
                "not be opened (%s) — scoped recovery will refuse", exc,
            )
            return None, None
        if _serves_recovery(candidate):
            return candidate, "database_url"

    return None, None
