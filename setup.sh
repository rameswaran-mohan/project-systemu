#!/usr/bin/env bash
set -euo pipefail

echo ""
echo " Systemu — Environment Setup"
echo " ============================"
echo ""

# Check Python 3.10+
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(sys.version_info >= (3, 10))" 2>/dev/null)
        if [ "$ver" = "True" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python 3.10+ not found. Install it and try again."
    exit 1
fi

echo "[INFO] Using $($PYTHON --version)"

# Create virtual environment
if [ -d ".venv" ]; then
    echo "[INFO] .venv already exists — skipping creation."
else
    echo "[INFO] Creating virtual environment in .venv ..."
    "$PYTHON" -m venv .venv
fi

# Upgrade pip
echo "[INFO] Upgrading pip ..."
.venv/bin/python -m pip install --upgrade pip --quiet

# Install dependencies
echo "[INFO] Installing dependencies from requirements.txt ..."
.venv/bin/pip install -r requirements.txt --quiet

# Install the package in editable mode so 'sharing_on' CLI is available
echo "[INFO] Installing Systemu package in editable mode ..."
.venv/bin/pip install -e . --quiet

echo ""
echo " Setup complete."
echo ""
echo " To start Systemu (venv mode — tools run as Python subprocesses):"
echo "   ./start.sh"
echo ""
echo " To start with Docker tool isolation (requires Docker):"
echo "   ./start_docker.sh"
echo ""
