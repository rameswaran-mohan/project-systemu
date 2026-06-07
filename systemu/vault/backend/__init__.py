"""systemu.vault.backend — storage dispatch layer for non-file vault backends.

Routes calls to sqlite_backend or postgres_backend depending on the vault's
``_storage_backend`` attribute (set by the vault factory at construction time,
or manually by test fixtures).

Supported values of ``_storage_backend``:
    'sqlite'   — lightweight sqlite3 dispatch (sqlite_backend.py)
    'postgres' — psycopg2 dispatch (postgres_backend.py)

'file' is handled directly by Vault methods in vault.py; calls never reach
this dispatch layer for the file backend.

Exported:
    dispatch_append_action_audit(vault, entry)
    dispatch_query_action_audit(vault, *, execution_id, since_ts, user_id)
    dispatch_append_session_summary(vault, summary)
    dispatch_query_session_summaries(vault, *, user_id, status, since_ts, limit)
    dispatch_search_session_summaries(vault, *, query, user_id, limit)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def dispatch_append_action_audit(vault, entry: Dict[str, Any]) -> None:
    """Route an append_action_audit call to the appropriate backend.

    Reads vault._storage_backend to select the implementation module,
    then delegates. Raises NotImplementedError for unknown backends.
    """
    backend = getattr(vault, "_storage_backend", "file")
    if backend == "sqlite":
        from systemu.vault.backend.sqlite_backend import (
            dispatch_append_action_audit as _sqlite,
        )
        return _sqlite(vault, entry)
    if backend == "postgres":
        from systemu.vault.backend.postgres_backend import (
            dispatch_append_action_audit as _pg,
        )
        return _pg(vault, entry)
    raise NotImplementedError(f"unknown storage backend: {backend!r}")


def dispatch_query_action_audit(
    vault,
    *,
    execution_id: str,
    since_ts: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Route a query_action_audit call to the appropriate backend.

    Reads vault._storage_backend to select the implementation module,
    then delegates. Raises NotImplementedError for unknown backends.
    """
    backend = getattr(vault, "_storage_backend", "file")
    if backend == "sqlite":
        from systemu.vault.backend.sqlite_backend import (
            dispatch_query_action_audit as _sqlite,
        )
        return _sqlite(vault, execution_id=execution_id,
                       since_ts=since_ts, user_id=user_id)
    if backend == "postgres":
        from systemu.vault.backend.postgres_backend import (
            dispatch_query_action_audit as _pg,
        )
        return _pg(vault, execution_id=execution_id,
                   since_ts=since_ts, user_id=user_id)
    raise NotImplementedError(f"unknown storage backend: {backend!r}")


def dispatch_append_session_summary(vault, summary) -> None:
    """Route an append_session_summary call to the appropriate backend."""
    backend = getattr(vault, "_storage_backend", "file")
    if backend == "sqlite":
        from systemu.vault.backend.sqlite_backend import (
            dispatch_append_session_summary as _sqlite,
        )
        return _sqlite(vault, summary)
    if backend == "postgres":
        from systemu.vault.backend.postgres_backend import (
            dispatch_append_session_summary as _pg,
        )
        return _pg(vault, summary)
    raise NotImplementedError(f"unknown storage backend: {backend!r}")


def dispatch_query_session_summaries(vault, *, user_id=None, status=None,
                                      since_ts=None, limit=None):
    """Route a query_session_summaries call to the appropriate backend."""
    backend = getattr(vault, "_storage_backend", "file")
    if backend == "sqlite":
        from systemu.vault.backend.sqlite_backend import (
            dispatch_query_session_summaries as _sqlite,
        )
        return _sqlite(vault, user_id=user_id, status=status,
                       since_ts=since_ts, limit=limit)
    if backend == "postgres":
        from systemu.vault.backend.postgres_backend import (
            dispatch_query_session_summaries as _pg,
        )
        return _pg(vault, user_id=user_id, status=status,
                   since_ts=since_ts, limit=limit)
    raise NotImplementedError(f"unknown storage backend: {backend!r}")


def dispatch_search_session_summaries(vault, *, query, user_id=None, limit=5):
    """Route a search_session_summaries call to the appropriate backend."""
    backend = getattr(vault, "_storage_backend", "file")
    if backend == "sqlite":
        from systemu.vault.backend.sqlite_backend import (
            dispatch_search_session_summaries as _sqlite,
        )
        return _sqlite(vault, query=query, user_id=user_id, limit=limit)
    if backend == "postgres":
        from systemu.vault.backend.postgres_backend import (
            dispatch_search_session_summaries as _pg,
        )
        return _pg(vault, query=query, user_id=user_id, limit=limit)
    raise NotImplementedError(f"unknown storage backend: {backend!r}")
