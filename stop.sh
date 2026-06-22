#!/usr/bin/env bash
# Systemu — mode-aware shutdown (POSIX).

set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .systemu_mode ]; then
    echo "[INFO] .systemu_mode missing — nothing to do."
    exit 0
fi

MODE="$(cat .systemu_mode | tr -d '[:space:]')"

stop_pid_file() {
    local pidfile="$1"
    local label="$2"
    if [ -f "$pidfile" ]; then
        local pid
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "[INFO] Stopping $label (PID $pid) …"
            kill "$pid" || true
            for _ in 1 2 3 4 5 6 7 8; do
                if ! kill -0 "$pid" 2>/dev/null; then break; fi
                sleep 1
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "[WARN] $label did not exit gracefully — sending SIGKILL"
                kill -9 "$pid" || true
            fi
        else
            echo "[INFO] $label PID $pid not running (stale)."
        fi
        rm -f "$pidfile"
    fi
}

case "$MODE" in
    local)
        stop_pid_file .systemu_worker.pid "worker"
        stop_pid_file .systemu_daemon.pid "daemon"
        echo "[INFO] Stopped."
        ;;
    docker-local)
        docker compose --profile local down
        ;;
    docker-enterprise)
        docker compose --profile enterprise down
        ;;
    *)
        echo "[ERROR] Unknown mode '$MODE' in .systemu_mode" >&2
        exit 1
        ;;
esac
