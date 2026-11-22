#!/usr/bin/env bash
# Linux / macOS launcher: creates .venv on first run, then starts the app.
set -euo pipefail
cd "$(dirname "$0")"

# Use a separate venv if .venv is a Windows one (same folder used from WSL and Windows)
VENV=.venv
[ -d .venv/Scripts ] && VENV=.venv-linux

if [ ! -x "$VENV/bin/python" ]; then
  PY="$(command -v python3 || command -v python || true)"
  if [ -z "$PY" ]; then
    echo "Python 3 not found. Install it (with tkinter) and try again." >&2
    exit 1
  fi
  if ! "$PY" -c "import tkinter" 2>/dev/null; then
    echo "Python's tkinter is missing. Install it, then run this again:" >&2
    echo "  Debian/Ubuntu: sudo apt install python3-tk python3-venv" >&2
    echo "  Fedora:        sudo dnf install python3-tkinter" >&2
    echo "  Arch:          sudo pacman -S tk" >&2
    exit 1
  fi
  echo "Creating virtual environment with $PY ..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r requirements.txt
fi

exec "$VENV/bin/python" adhoc_share.py
