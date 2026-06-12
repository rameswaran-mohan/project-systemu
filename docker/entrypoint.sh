#!/bin/sh
# Systemu container entrypoint.
#
# Separates runtime vault data from the Python `systemu.vault` module code.
#
# Why this exists:
#   docker-compose mounts a named volume at SYSTEMU_VAULT_DIR so data persists
#   across container restarts.  If that path overlapped the Python module
#   directory (/app/systemu/vault/), the volume would shadow factory.py /
#   vault.py / *.py from the image, breaking imports and freezing the codebase
#   at whatever the volume captured on first init.
#
#   Instead the image bakes starter content at /app/starter-vault/ (a stable
#   read-only path) and the volume mounts at /data/vault (data-only).  On
#   first boot this script seeds /data/vault from /app/starter-vault/ so
#   fresh installs still get the starter tools + skills + shadow army.
#
# Behaviour:
#   - $SYSTEMU_VAULT_DIR empty  → copy starter content in
#   - $SYSTEMU_VAULT_DIR has content → leave it alone (user data wins)
#   - Always honour the $@ args so this acts as a transparent wrapper

set -e

VAULT_DIR="${SYSTEMU_VAULT_DIR:-/data/vault}"
STARTER_DIR="${SYSTEMU_STARTER_VAULT_DIR:-/app/starter-vault}"

mkdir -p "$VAULT_DIR"

# Seed only when the vault has no data — checked by looking for index.json
# files which the starter content always ships with.  This is more reliable
# than `ls` against arbitrary user-added files.
if [ -d "$STARTER_DIR" ] && [ ! -f "$VAULT_DIR/shadow_army/index.json" ]; then
    echo "[entrypoint] Seeding vault from $STARTER_DIR → $VAULT_DIR (first boot)"
    cp -rn "$STARTER_DIR"/. "$VAULT_DIR"/
fi

# v0.6.6-b: idempotent alembic upgrade.
#
# Closes the alembic_version-table-never-populated bug found in the
# 2026-05-19 docker E2E (captures/E2E_VERDICT_DOCKER.md, finding B).
# Without this step, fresh installs got their schema via SQLAlchemy's
# create_all() on first save, but alembic_version was never populated.
# The NEXT migration would then fail with "relation already exists"
# because alembic had no version to compare against.
#
# Idempotent: alembic upgrade head is a no-op when at head.
# Soft-fail: daemon still starts via SQLAlchemy create_all() if alembic
#            errors, so a broken migration doesn't block the container.
if [ -n "${SYSTEMU_DATABASE_URL:-}" ]; then
    echo "[entrypoint] Running alembic upgrade head ..."
    (cd /app && alembic upgrade head 2>&1) || {
        # W13.7 (docker A6 audit): upgrade failed (typically a half-created
        # alembic_version from a prior boot on a persisted volume). The
        # daemon's create_all builds the CURRENT model schema (= head), so
        # stamping head records the truthful baseline and future migrations
        # apply as deltas instead of failing forever.
        echo "[entrypoint] alembic upgrade failed — stamping head (create_all builds current schema)"
        (cd /app && alembic stamp head 2>&1) \
            || echo "[entrypoint] stamp also failed — daemon falls back to SQLAlchemy create_all only"
    }
fi

exec "$@"
