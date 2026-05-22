#!/usr/bin/env bash
set -euo pipefail

if [ ! -d ".venv" ]; then
    echo "[ERROR] No virtual environment found. Run ./setup.sh first."
    exit 1
fi

if ! command -v docker &>/dev/null; then
    echo "[ERROR] Docker not found on PATH. Install Docker and try again."
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "[ERROR] Docker daemon is not running. Start it and try again."
    exit 1
fi

source .venv/bin/activate

echo "[INFO] Starting Systemu daemon (Docker tool-sandbox mode) ..."
SYSTEMU_TOOL_BACKEND=docker sharing_on daemon start "$@"
