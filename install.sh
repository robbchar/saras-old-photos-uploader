#!/usr/bin/env bash
# Converges this machine to whatever the current checkout needs. Safe to re-run;
# this is the upgrade path too: git pull && ./install.sh
set -euo pipefail

MIN_PY_MAJOR=3
MIN_PY_MINOR=10

cd "$(dirname "$0")"

qualifies() {
  "$1" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" 2>/dev/null
}

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && qualifies "$candidate"; then
    PYTHON="$candidate"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  echo "No Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ on PATH. macOS ships 3.9.6, which the" >&2
  echo "Google libraries do not support. Install one, then re-run this script:" >&2
  echo "  brew install python@3.12" >&2
  echo "See docs/DEPLOYMENT.md, section \"Python\"." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "creating .venv with $PYTHON"
  "$PYTHON" -m venv .venv
fi

./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt

exec ./.venv/bin/python ia_bulk.py setup "$@"
