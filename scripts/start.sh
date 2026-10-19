#!/usr/bin/env bash
# Start Autobench in the background: web UI + REST API + MCP server + background jobs (one process).
#   autobench start [--port N] [--rebuild] [--fg]   (or scripts/start.sh)
#     --rebuild  force a fresh web UI build (otherwise it rebuilds only when web/ sources changed)
#     --fg       run in the foreground (Ctrl-C to stop) instead of in the background
set -euo pipefail
# resolve symlinks (e.g. ~/.local/bin/autobench-start) to find the repo
SELF="${BASH_SOURCE[0]}"
while [[ -L "$SELF" ]]; do t="$(readlink "$SELF")"; [[ "$t" == /* ]] && SELF="$t" || SELF="$(dirname "$SELF")/$t"; done
source "$(dirname "$SELF")/_common.sh"

PORT="" REBUILD=0 FG=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --rebuild) REBUILD=1; shift ;;
    --fg) FG=1; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
PORT="${PORT:-$(default_port)}"

if pid="$(running_pid)"; then
  echo "Autobench is already running (pid $pid). Use 'autobench stop' or 'autobench restart'."
  exit 0
fi
if pid="$(port_pid "$PORT")"; then
  echo "An Autobench server is already listening on port $PORT (pid $pid, not started by this script)."
  echo "Stop it with: autobench stop --port $PORT"
  exit 1
fi
if lsof -ti "tcp:$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is in use by another program. Pick another: autobench start --port 8001" >&2
  exit 1
fi

[[ -f "$ROOT/.env" ]] || echo "note: no .env found – market data falls back to free providers (see .env.example)"

# 1. Python environment (first run only)
if [[ ! -x "$VENV/bin/autobench" ]]; then
  echo "Setting up the Python environment…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$BACKEND"
fi

# 2. Web UI build (when missing or stale)
BUNDLE="$WEB/build/main.dart.js"
stale=0
if [[ $REBUILD -eq 1 || ! -f "$BUNDLE" ]]; then
  stale=1
elif [[ -n "$(find "$WEB/lib" "$WEB/static" "$WEB/tool" "$WEB/pubspec.yaml" -newer "$BUNDLE" -print -quit)" ]]; then
  stale=1
fi
if [[ $stale -eq 1 ]]; then
  if DART="$(find_dart)"; then
    echo "Building the web UI…"
    (cd "$WEB" && { [[ -d .dart_tool ]] || "$DART" pub get >/dev/null; } && "$DART" run tool/build.dart)
  elif [[ -f "$BUNDLE" ]]; then
    echo "warning: Dart SDK not found; serving the existing (possibly outdated) UI build" >&2
  else
    echo "error: the web UI is not built and the Dart SDK was not found (https://dart.dev/get-dart)" >&2
    exit 1
  fi
fi

# 3. Server
mkdir -p "$RUN_DIR"
if [[ $FG -eq 1 ]]; then
  echo "Autobench on http://127.0.0.1:$PORT (MCP: /mcp) – Ctrl-C to stop"
  cd "$BACKEND" && exec "$VENV/bin/autobench" serve --port "$PORT"
fi

cd "$BACKEND"
nohup "$VENV/bin/autobench" serve --port "$PORT" >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"

printf "Starting"
for _ in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo
    echo "Autobench is up (pid $(cat "$PID_FILE"))"
    echo "  Web UI:  http://127.0.0.1:$PORT"
    echo "  MCP:     http://127.0.0.1:$PORT/mcp"
    echo "  Logs:    $LOG_FILE"
    exit 0
  fi
  if ! running_pid >/dev/null; then
    echo; echo "error: the server exited during startup. Last log lines:" >&2
    tail -20 "$LOG_FILE" >&2
    exit 1
  fi
  printf "."; sleep 0.5
done
echo; echo "error: the server did not answer within 30s; see $LOG_FILE" >&2
exit 1
