#!/bin/sh
# Singularity installer wrapper (macOS/Linux). All logic lives in install.py.
set -e
ENGINE="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ENGINE/.venv"
if [ ! -x "$VENV/bin/python" ]; then
    echo "[install] creating venv at $VENV"
    python3 -m venv "$VENV"
fi
exec "$VENV/bin/python" "$ENGINE/install/install.py" "$@"
