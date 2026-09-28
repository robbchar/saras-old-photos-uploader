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

# Real uploads land in the registry's ia_collection for this project, which
# need not equal the project id. Resolve it for the banner, falling back to the
# project id if the registry can't be read (for example before the venv exists).
COLLECTION="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["projects"][sys.argv[2]]["ia_collection"])' "$REGISTRY" "$PROJECT" 2>/dev/null || printf '%s' "$PROJECT")"

banner() {
  printf '\n'
  printf '  ==================================================\n'
  printf '   LCPS upload page  -  LIVE MODE\n'
  printf '   Real uploads go to the "%s" collection.\n' "$COLLECTION"
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

# If a server is already listening on this port - an earlier launch still
# running - starting a second one would only abort with an "address already in
# use" traceback. Point the browser at the page already up, and stop here.
if curl -sf -o /dev/null "$URL"; then
  printf 'The upload page is already running at %s\n' "$URL"
  printf 'Opening it; to restart, close its Terminal window first.\n'
  open "$URL" || true
  pause_then_exit 0
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
  opened=""
  for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "$URL"; then
      open "$URL"
      opened=1
      break
    fi
    sleep 0.5
  done
  if [ -z "$opened" ]; then
    printf '\nCould not reach the server at %s after ~15s.\n' "$URL" >&2
    printf 'If it is still starting, open that URL in your browser by hand.\n' >&2
  fi
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

# The server is done, so the opener has no reason to keep polling. Stop it now
# so a refusal (the server never listened) can't later print a stale "still
# starting" note over the "did not start" message below.
[ -n "$OPENER_PID" ] && kill "$OPENER_PID" 2>/dev/null || true

# Keep the window open so the reason stays readable instead of the window
# vanishing. An exit of 0 means the server never served: run_server returns 0
# after refusing to start (missing or stale bundle, unknown project, bad
# registry), and printed why above. A stop after it did serve arrives as a
# signal (non-zero), so the two cases read differently here.
if [ "$status" -eq 0 ]; then
  printf '\nServer did not start - see the reason above.\n'
else
  printf '\nServer stopped (exit %s).\n' "$status"
fi
pause_then_exit "$status"
