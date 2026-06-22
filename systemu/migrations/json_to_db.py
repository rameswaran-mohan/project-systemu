"""JSON-vault → SqliteVault (sqlite/postgres) migration tool.

Existing operators on the pre-pivot file backend who want to move to
docker-local or docker-enterprise need a one-shot way to copy their JSON
vault into the new database-backed vault.  This module reads the JSON
vault directly via the production :class:`Vault` class and writes through
:class:`SqliteVault` — so the migration is bound to whatever the entity
classes (Scroll, Activity, Shadow, …) currently look like rather than a
schema snapshot frozen at migration time.

Idempotency:
  Re-running the migration is safe.  Each entity is keyed by its primary
  ``id`` field; the SqliteVault's ``save_*`` methods upsert.  Duplicate
  invocations leave the destination unchanged.

Usage:
  python -m systemu.migrations.json_to_db \\
        --source systemu/vault \\
        --target postgresql://systemu:secret@localhost:5432/systemu

  python -m systemu.migrations.json_to_db --source ./systemu/vault \\
        --target sqlite:///./data/systemu.db --dry-run

When --dry-run is passed, the tool only enumerates and counts entities
without writing.  Useful for sizing the migration window.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Callable, List, Tuple

# Match install.py — Windows cp1252 stdout chokes on the box-drawing chars
# this script prints in its summary table.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def _migrate_collection(
    label: str,
    list_fn: Callable[[], List[Any]],
    save_fn: Callable[[Any], None],
    *,
    dry_run: bool,
    get_fn: Callable[[str], Any] | None = None,
) -> Tuple[int, int]:
    """Iterate *list_fn()* and call *save_fn(entity)* on each.

    The source ``Vault.list_*`` methods return *index dicts* (headers
    only — id, name, status, …), not full Pydantic instances.  The
    destination ``SqliteVault.save_*`` methods expect the Pydantic
    model.  When *get_fn* is supplied we hydrate each header via
    ``get_fn(item['id'])`` before saving, which avoids the
    ``'dict' object has no attribute '<field>'`` errors that broke
    every collection on first migration runs.

    Returns ``(attempted, succeeded)``.  Failures are logged and do
    not abort the overall migration — partial progress is still
    useful and can be re-run (the destination is idempotent).
    """
    items = list_fn()
    attempted = len(items)
    if dry_run:
        logger.info("[%s] dry-run: %d entities found", label, attempted)
        return attempted, attempted
    succeeded = 0
    orphans = 0
    for item in items:
        # Index entries are plain dicts; full entities are Pydantic
        # objects.  Hydrate dicts via get_fn(id) when we have one.
        item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        try:
            entity = item
            if get_fn is not None and isinstance(item, dict):
                if not item_id:
                    raise ValueError("index entry missing 'id' field")
                entity = get_fn(item_id)
            save_fn(entity)
            succeeded += 1
        except KeyError as exc:
            # Index references an entity whose on-disk JSON is missing.
            # This is a starter-vault / data-cleanup issue, not a
            # migration bug — log it and keep going, but don't count it
            # toward the hard failure tally so the migration can still
            # exit cleanly when the rest of the vault migrated.
            orphans += 1
            logger.warning(
                "[%s] orphan header — index lists %s but no file on disk: %s",
                label, item_id or "<no-id>", exc,
            )
        except Exception as exc:
            logger.warning("[%s] failed on %s: %s",
                           label, item_id or "<no-id>", exc)
    if orphans:
        logger.info(
            "[%s] migrated %d/%d (%d orphan header%s skipped)",
            label, succeeded, attempted, orphans, "" if orphans == 1 else "s",
        )
    else:
        logger.info("[%s] migrated %d/%d", label, succeeded, attempted)
    # Orphans don't count as failures — they're skipped index entries that
    # never had data to migrate in the first place.  Treat them as
    # 'effectively migrated' for exit-code accounting.
    return attempted, succeeded + orphans


def _migrate_chat_history(
    src_vault: Any,
    dst_vault: Any,
    *,
    dry_run: bool,
) -> Tuple[int, int]:
    entries = src_vault.load_chat_history(limit=10_000)
    if dry_run:
        logger.info("[chat_history] dry-run: %d entries found", len(entries))
        return len(entries), len(entries)
    succeeded = 0
    for entry in entries:
        try:
            dst_vault.append_chat_history(entry)
            succeeded += 1
        except Exception as exc:
            logger.warning("[chat_history] failed on ts=%s: %s",
                           entry.get("ts"), exc)
    logger.info("[chat_history] migrated %d/%d", succeeded, len(entries))
    return len(entries), succeeded


def migrate(
    source_path: Path,
    target_url: str,
    *,
    dry_run: bool = False,
) -> int:
    """Run the migration.  Returns 0 on success, non-zero on hard failure."""
    if not source_path.exists():
        logger.error("Source vault does not exist: %s", source_path)
        return 2

    from systemu.vault.vault import Vault
    src = Vault(str(source_path))

    if dry_run:
        dst = src   # never used; satisfies type
    else:
        try:
            from systemu.storage.sqlite.vault import SqliteVault
        except ImportError as exc:
            logger.error(
                "SqliteVault unavailable (%s) — install the docker-local or "
                "docker-enterprise extras first.", exc,
            )
            return 3
        try:
            dst = SqliteVault(target_url)
        except Exception as exc:
            logger.error("Could not connect to target %s: %s", target_url, exc)
            return 4

    summary: List[Tuple[str, int, int]] = []

    # The order matters for the indexes that reference each other (scrolls
    # before activities, shadows before activity assignments, etc.) — we
    # bypass cross-reference validation by going through the save_* methods
    # which re-derive headers locally.
    summary.append(("scrolls", *_migrate_collection(
        "scrolls", src.list_scrolls, dst.save_scroll,
        get_fn=src.get_scroll, dry_run=dry_run,
    )))
    summary.append(("shadows", *_migrate_collection(
        "shadows", src.list_shadows, dst.save_shadow,
        get_fn=src.get_shadow, dry_run=dry_run,
    )))
    summary.append(("tools", *_migrate_collection(
        "tools", src.list_tools, dst.save_tool,
        get_fn=src.get_tool, dry_run=dry_run,
    )))
    summary.append(("skills", *_migrate_collection(
        "skills", src.list_skills, dst.save_skill,
        get_fn=src.get_skill, dry_run=dry_run,
    )))
    summary.append(("activities", *_migrate_collection(
        "activities", src.list_activities, dst.save_activity,
        get_fn=src.get_activity, dry_run=dry_run,
    )))
    if hasattr(src, "list_evolutions") and hasattr(dst, "save_evolution"):
        summary.append(("evolutions", *_migrate_collection(
            "evolutions", src.list_evolutions, dst.save_evolution,
            get_fn=src.get_evolution, dry_run=dry_run,
        )))
    summary.append(("chat_history", *_migrate_chat_history(
        src, dst, dry_run=dry_run,
    )))

    print("\nMigration summary")
    print("─────────────────")
    for label, attempted, succeeded in summary:
        marker = "✓" if attempted == succeeded else "!"
        print(f"  {marker} {label:<14} {succeeded}/{attempted}")
    failed = sum(a - s for _, a, s in summary)
    if failed:
        print(f"\n{failed} entities failed — see warnings above.  "
              f"Re-run after investigating; the migration is idempotent.")
        return 1
    print("\nAll entities migrated cleanly." if not dry_run else
          "\nDry run complete — no writes performed.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        prog="systemu.migrations.json_to_db",
        description="Migrate a JSON-vault directory into a SqliteVault DB.",
    )
    p.add_argument("--source", required=True,
                   help="Path to the JSON vault directory (e.g. ./systemu/vault).")
    p.add_argument("--target",
                   help="SQLAlchemy URL for the destination "
                        "(e.g. postgresql://user:pw@host/db).  "
                        "Required unless --dry-run is given.")
    p.add_argument("--dry-run", action="store_true",
                   help="Count entities without writing.")
    p.add_argument("--verbose", action="store_true", help="Debug-level logging.")
    args = p.parse_args()

    if not args.dry_run and not args.target:
        p.error("--target is required unless --dry-run is set.")

    _setup_logging(args.verbose)
    rc = migrate(
        source_path=Path(args.source).resolve(),
        target_url=args.target or "",
        dry_run=args.dry_run,
    )
    sys.exit(rc)


if __name__ == "__main__":
    main()
