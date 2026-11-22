#!/usr/bin/env bash
# Linux / macOS launcher: creates .venv on first run, then starts the app.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  PY="$(command -v python3 || command -v python || true)"
  if [ -z "$PY" ]; then
    echo "Python 3 not found. Install it (with tkinter) and try again." >&2
    exit 1
  fi
  echo "Creating virtual environment with $PY ..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi

exec .venv/bin/python adhoc_share.py
