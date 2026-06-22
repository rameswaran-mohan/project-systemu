#!/usr/bin/env bash
# Systemu — mode-aware launcher (POSIX).
#
# Reads .systemu_mode (written by install.py) and brings up the right runtime:
#
#   local              Detached daemon + worker subprocesses on the host.
#                      PIDs in .systemu_daemon.pid and .systemu_worker.pid.
#   docker-local       docker compose --profile local up -d
#   docker-enterprise  docker compose --profile enterprise up -d

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .systemu_mode ]; then
    echo "[ERROR] .systemu_mode missing — run ./install.sh first." >&2
    exit 1
fi

MODE="$(cat .systemu_mode | tr -d '[:space:]')"

start_local() {
    if [ ! -d .venv ]; then
        echo "[ERROR] .venv missing — run ./install.sh." >&2
        exit 1
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate
    mkdir -p logs

    # Source .env so SYSTEMU_* / OPENROUTER_API_KEY / etc. are in the
    # environment before spawning daemon + worker subprocesses.  The
    # sharing_on CLI and worker.py also auto-load .env via python-dotenv
    # as a belt-and-braces measure, but exporting here means subshells and
    # any future tools spawned in this session also see the values.
    if [ -f .env ]; then
        set -a
        # shellcheck disable=SC1091
        source .env
        set +a
    fi

    # v0.6.1+: idempotent schema check.  Protects users who `git pull` to a
    # release with a new migration but skip re-running ./install.sh.  Without
    # this, the daemon crashes at first DB read with a cryptic
    # `sqlalchemy.exc.OperationalError: no such column: ...` error.
    # scripts/upgrade_db.py loads .env so alembic sees SYSTEMU_DATABASE_URL.
    echo "[INFO] Verifying DB schema (alembic upgrade head) …"
    if ! python scripts/upgrade_db.py 2>>logs/alembic.log; then
        echo "[WARN] DB schema upgrade failed — see logs/alembic.log." >&2
        echo "[WARN] Continuing anyway; daemon may crash on schema mismatch." >&2
    fi

    # Daemon (NiceGUI dashboard + APScheduler).
    # Note: we call `python -m sharing_on daemon …` instead of the
    # `sharing_on` console-script.  The console-script .exe shim that
    # pip creates in `.venv/Scripts/` is cached against an older import of
    # the sharing_on.cli module and silently drops 8 of the 11 click
    # commands — including `daemon` — on Windows + some Linux setups.
    # Running the module directly always picks up the current source.
    if [ -f .systemu_daemon.pid ] && kill -0 "$(cat .systemu_daemon.pid)" 2>/dev/null; then
        echo "[INFO] Daemon already running (PID $(cat .systemu_daemon.pid))."
    else
        echo "[INFO] Starting daemon (NiceGUI + scheduler) …"
        nohup python -m sharing_on daemon start --foreground >> logs/daemon.log 2>&1 &
        echo $! > .systemu_daemon.pid
        echo "[INFO] Daemon PID $(cat .systemu_daemon.pid)"
    fi

    # Worker (Huey consumer over SQLite broker)
    if [ -f .systemu_worker.pid ] && kill -0 "$(cat .systemu_worker.pid)" 2>/dev/null; then
        echo "[INFO] Worker already running (PID $(cat .systemu_worker.pid))."
    else
        echo "[INFO] Starting worker (Huey consumer) …"
        nohup python -m systemu.worker >> logs/worker.log 2>&1 &
        echo $! > .systemu_worker.pid
        echo "[INFO] Worker PID $(cat .systemu_worker.pid)"
    fi

    echo
    echo " Dashboard:  http://localhost:8765/"
    echo " Logs:       logs/daemon.log  &  logs/worker.log"
    echo " Stop:       ./stop.sh"
}

start_compose() {
    local profile="$1"
    if ! command -v docker >/dev/null; then
        echo "[ERROR] docker not on PATH." >&2
        exit 1
    fi
    docker compose --profile "$profile" up -d
    echo
    echo " Dashboard: http://localhost:${SYSTEMU_PORT:-8765}/"
    echo " Logs:      docker compose --profile $profile logs -f"
    echo " Stop:      ./stop.sh"
}

case "$MODE" in
    local)              start_local                  ;;
    docker-local)       start_compose local          ;;
    docker-enterprise)  start_compose enterprise     ;;
    *)
        echo "[ERROR] Unknown mode '$MODE' in .systemu_mode" >&2
        exit 1
        ;;
esac
