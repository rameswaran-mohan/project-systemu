#!/usr/bin/env bash
# Systemu — one-command upgrade (POSIX).
#
# Stops the daemon, pulls latest code, reinstalls deps, runs alembic
# migrations, restarts.  Refuses on a dirty tree or non-fast-forward pull.
#
# Usage:
#   ./update.sh           # interactive (asks before stopping daemon)
#   ./update.sh --yes     # non-interactive (CI / cron)

set -euo pipefail
cd "$(dirname "$0")"

YES=0
for arg in "$@"; do
    case "$arg" in
        -y|--yes) YES=1 ;;
        -h|--help)
            echo "Usage: $0 [--yes]"
            echo "  --yes   Skip the 'stop daemon' confirmation prompt."
            exit 0
            ;;
        *) echo "[ERROR] Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# ── Pre-flight checks ──────────────────────────────────────────────────────

if [ ! -d .git ]; then
    echo "[ERROR] $(pwd) is not a git checkout — refusing to update." >&2
    exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "[ERROR] Working tree has uncommitted changes." >&2
    echo "        Commit / stash them first, then re-run ./update.sh." >&2
    exit 1
fi

if [ ! -d .venv ]; then
    echo "[ERROR] .venv missing — run ./install.sh first." >&2
    exit 1
fi

# ── Confirmation ───────────────────────────────────────────────────────────

if [ "$YES" -ne 1 ]; then
    echo "About to stop daemon + worker, pull latest, reinstall deps, migrate DB, restart."
    printf "Continue? [y/N] "
    read -r ans
    case "${ans:-}" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
fi

# ── Stop ───────────────────────────────────────────────────────────────────

if [ -x ./stop.sh ]; then
    echo "[INFO] Stopping daemon + worker …"
    ./stop.sh || echo "[WARN] stop.sh returned non-zero; continuing."
else
    echo "[WARN] stop.sh not found / not executable; skipping stop."
fi

# ── Pull ───────────────────────────────────────────────────────────────────

echo "[INFO] git pull --ff-only …"
if ! git pull --ff-only; then
    echo "[ERROR] git pull failed (non-fast-forward or network error)." >&2
    echo "        Resolve manually, then re-run ./update.sh." >&2
    exit 1
fi

# ── Reinstall deps ─────────────────────────────────────────────────────────

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[INFO] Upgrading pip + dependencies …"
pip install --upgrade pip --quiet
pip install -r requirements.txt --upgrade --quiet
pip install -e ".[local]" --quiet

# ── Migrate ────────────────────────────────────────────────────────────────

echo "[INFO] Running alembic upgrade head …"
if ! python scripts/upgrade_db.py; then
    echo "[WARN] DB migration failed — see logs/alembic.log." >&2
    echo "       The daemon may crash on schema mismatch.  Re-run install.sh" >&2
    echo "       if the failure persists." >&2
fi

# ── Restart ────────────────────────────────────────────────────────────────

if [ -x ./start.sh ]; then
    echo "[INFO] Starting daemon + worker …"
    ./start.sh
else
    echo "[WARN] start.sh not found; start manually." >&2
fi

echo
echo "[INFO] Update complete."
