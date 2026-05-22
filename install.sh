#!/usr/bin/env bash
# Systemu — bootstrap installer (POSIX).
#
# Verifies a system Python 3.10+ is present, then hands off to install.py
# which does the real work (mode prompt, venv creation or docker setup, etc.).

set -euo pipefail

cd "$(dirname "$0")"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python 3.10+ not found on PATH. Install it and re-run." >&2
    exit 1
fi

exec "$PYTHON" install.py "$@"
