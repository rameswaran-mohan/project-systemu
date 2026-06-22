"""v0.6.1+: idempotent schema migration helper used by start.sh / start.bat.

Loads .env (so SYSTEMU_DATABASE_URL points to the right place) and runs
``alembic upgrade head``.  A no-op when the DB is already at head.

This exists because:
  * The daemon itself does NOT auto-run alembic on startup.
  * Users who ``git pull`` a release with a new migration but skip
    re-running install.py would otherwise hit cryptic
    ``OperationalError: no such column: ...`` errors.
  * Calling ``alembic upgrade head`` directly from start.bat / start.sh
    is awkward because alembic doesn't auto-load .env — env vars wouldn't
    be set.  This script handles that loading.

Exit codes:
  0 — schema is at head (no-op or upgrade applied)
  1 — upgrade failed; stderr contains alembic's error
  2 — couldn't load .env or other setup error
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent

    # Load .env if present (idempotent — if env vars already set, .env doesn't override).
    try:
        from dotenv import load_dotenv
        load_dotenv(repo_root / ".env")
    except ImportError:
        print(
            "[upgrade_db] python-dotenv not installed — falling back to existing env",
            file=sys.stderr,
        )

    db_url = os.environ.get("SYSTEMU_DATABASE_URL")
    if not db_url:
        # Default for local mode (matches install.py)
        db_url = f"sqlite:///{(repo_root / 'data' / 'systemu.db').as_posix()}"
        os.environ["SYSTEMU_DATABASE_URL"] = db_url

    try:
        from alembic.config import Config
        from alembic import command
    except ImportError as exc:
        print(f"[upgrade_db] alembic not installed: {exc}", file=sys.stderr)
        return 2

    cfg_path = repo_root / "alembic.ini"
    if not cfg_path.exists():
        print(f"[upgrade_db] alembic.ini missing at {cfg_path}", file=sys.stderr)
        return 2

    cfg = Config(str(cfg_path))
    # Force alembic to use the env-derived URL, overriding whatever alembic.ini says
    cfg.set_main_option("sqlalchemy.url", db_url)

    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        print(f"[upgrade_db] upgrade failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
