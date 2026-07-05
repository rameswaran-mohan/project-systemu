"""DEC-9 SnapshotMigrator — version the ExecutionSnapshot on-disk schema.

Leaf module: stdlib-only, NO systemu imports, so `execution_snapshot` (and
anything else) can import it without a cycle. Migrations are pure dict->dict,
versioned with the release that adds a field:

  * schema_version == CURRENT  -> fast path, return unchanged (no backup)
  * schema_version  < CURRENT   -> back up the file once (`.bak`), then apply
                                   each registered migration in ascending order,
                                   stamping schema_version after each step
  * schema_version  > CURRENT   -> raise SnapshotRefused (honest refusal; the
                                   caller must NOT silently start fresh, which
                                   could re-execute effectful actions)

The migrator NEVER rewrites the original snapshot file — it transforms the
in-memory dict and (defensively) copies the original to `.bak`. The migrated
snapshot is re-persisted by the run's next write_snapshot at CURRENT version.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Bump this whenever a migration is added. v1 = pre-G1 (unversioned);
# v2 = G1 adds objective_graph + next_objective_id.
CURRENT_SCHEMA_VERSION = 2


class SnapshotRefused(Exception):
    """A snapshot's schema_version is newer than this build understands.

    Resume must refuse honestly rather than silently start fresh.
    """

    def __init__(self, version: int, current: int, path: Optional[Path] = None):
        self.version = version
        self.current = current
        self.path = path
        super().__init__(
            f"snapshot schema {version!r} is unsupported by this build "
            f"(current v{current})"
            + (f" ({path})" if path else "")
            + " — refusing to resume"
        )


def _migrate_1_to_2(data: Dict[str, Any]) -> Dict[str, Any]:
    """v1 -> v2 (G1): add the objective-graph carrier keys, defaulted empty."""
    data.setdefault("objective_graph", [])
    data.setdefault("next_objective_id", 1)
    return data


# Registry: {from_version: fn}. Keys must be contiguous from 1..CURRENT-1.
_MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    1: _migrate_1_to_2,
}


def migrate_snapshot_dict(
    data: Dict[str, Any], *, path: Optional[Path] = None
) -> Dict[str, Any]:
    """Migrate a raw snapshot dict up to CURRENT_SCHEMA_VERSION.

    Raises SnapshotRefused if the snapshot is newer than this build supports.
    """
    raw_version = data.get("schema_version", 1)
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        # A present-but-unintelligible schema_version is an "I can't understand this
        # snapshot" case — refuse loudly (same posture as newer-than-supported),
        # never let a raw ValueError/TypeError leak into the caller's blanket except
        # and degrade to a silent fresh-start (DEC-9).
        raise SnapshotRefused(raw_version, CURRENT_SCHEMA_VERSION, path)
    if version < 1:
        # Versions are 1-based; a sub-1 value is corrupt/unintelligible.
        raise SnapshotRefused(version, CURRENT_SCHEMA_VERSION, path)
    if version == CURRENT_SCHEMA_VERSION:
        return data
    if version > CURRENT_SCHEMA_VERSION:
        raise SnapshotRefused(version, CURRENT_SCHEMA_VERSION, path)

    # version < CURRENT: back up the original ONCE before applying migrations.
    if path is not None:
        try:
            p = Path(path)
            if p.exists():
                shutil.copy2(p, p.with_suffix(p.suffix + ".bak"))
        except Exception:
            pass  # best-effort backup; never block a valid migration on it

    while version < CURRENT_SCHEMA_VERSION:
        migrate = _MIGRATIONS.get(version)
        if migrate is None:
            # A gap in the chain — cannot safely migrate. Refuse honestly.
            raise SnapshotRefused(version, CURRENT_SCHEMA_VERSION, path)
        data = migrate(data)
        version += 1
        data["schema_version"] = version
    return data
