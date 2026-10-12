#!/usr/bin/env bash
# Math Checker V5 - Linux/macOS launcher
# Usage: ./start.sh [port]     (default port: 8642)
#
# Prefers a bundled runtime (portable install) then a virtual environment,
# then a system Python. In portable mode (a portable.flag file beside the app)
# all data is kept next to the application.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if [[ -x ./runtime/bin/python3 ]]; then
    PYTHON=./runtime/bin/python3
  elif [[ -x ./.venv/bin/python ]]; then
    PYTHON=./.venv/bin/python
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  elif command -v python >/dev/null 2>&1; then
    PYTHON=python
  else
    echo "Python 3.12+ is required. Install it, then run this file again." >&2
    exit 1
  fi
fi

if [[ -f ./portable.flag ]]; then
  export MC5_DATA_DIR="$PWD/data"
fi

exec "$PYTHON" main.py "$@"
