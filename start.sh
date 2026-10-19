#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo ""
echo " === Partner ride bot ==="
echo ""

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 was not found. Install Python 3.11+ and try again."
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating virtual environment..."
  "$PY" -m venv .venv
fi

VPY=.venv/bin/python
echo "Installing / updating packages..."
"$VPY" -m pip install --upgrade pip >/dev/null
"$VPY" -m pip install -r requirements.txt

echo ""
exec "$VPY" run.py "$@"
