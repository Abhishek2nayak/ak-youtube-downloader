#!/usr/bin/env bash
# AK YouTube Downloader — local launcher (macOS / Linux)
set -e
cd "$(dirname "$0")"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys;exit(0 if sys.version_info[0]==3 else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done

if [ -z "$PY" ]; then
  echo "Python 3 is not installed."
  echo "  macOS:  brew install python"
  echo "  Ubuntu: sudo apt install python3 python3-venv"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating environment (one time)..."
  "$PY" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip --quiet
echo "Installing dependencies..."
.venv/bin/python -m pip install -r requirements.txt --quiet

echo
.venv/bin/python app.py
