#!/bin/bash
#
# One-click LIVE launcher for the LCPS upload page (macOS).
#
# Serves the already-built upload page for real Internet Archive uploads.
# The page bundle (upload_page/dist) is built and committed by a developer
# before deployment, so this Mac needs only the Python venv - no Node, no
# yarn. Keep the checkout up to date with `git pull` and the built bundle
# comes with it.
#
# Double-click this file, or use the Dock button described in
# docs/MAC-LAUNCHER.md. Close the Terminal window to stop the server.

set -euo pipefail

# Resolve the repo root as this script's own directory, so the launcher
# works wherever the checkout lives.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

PROJECT="sarasoldphotos"
REGISTRY="projects_registry.json"
PORT="5277"
URL="http://127.0.0.1:${PORT}"
PYTHON="${REPO_ROOT}/.venv/bin/python"

banner() {
  printf '\n'
  printf '  ==================================================\n'
  printf '   LCPS upload page  -  LIVE MODE\n'
  printf '   Real uploads go to the "%s" collection.\n' "$PROJECT"
  printf '   Close this window to stop the server.\n'
  printf '  ==================================================\n\n'
}

pause_then_exit() {
  printf '\nPress return to close this window.'
  read -r _ || true
  exit "${1:-0}"
}

banner

if [ ! -x "$PYTHON" ]; then
  printf 'ERROR: no Python virtualenv found at:\n  %s\n' "$PYTHON" >&2
  printf 'Run ./install.sh first (see docs/DEPLOYMENT.md, section 10).\n' >&2
  pause_then_exit 1
fi

# Stop the server (and the browser opener) when this window closes or on
# Ctrl-C. Only our own child processes are killed, never by name.
SERVER_PID=""
OPENER_PID=""
cleanup() {
  [ -n "$OPENER_PID" ] && kill "$OPENER_PID" 2>/dev/null || true
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

# Open the browser once the server is actually listening (poll up to ~15s).
(
  for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "$URL"; then
      open "$URL"
      break
    fi
    sleep 0.5
  done
) &
OPENER_PID=$!

# Serve in the foreground. --live means real, permanent uploads.
"$PYTHON" ia_bulk.py serve \
  --project "$PROJECT" \
  --registry "$REGISTRY" \
  --live \
  --port "$PORT" &
SERVER_PID=$!

set +e
wait "$SERVER_PID"
status=$?
set -e

# If the server stopped on its own (for example a startup refusal because the
# bundle is missing or stale), keep the window open so the reason stays
# readable instead of the window vanishing.
printf '\nServer stopped (exit %s).\n' "$status"
pause_then_exit "$status"
