#!/usr/bin/env bash
# Stop the Autobench server started by scripts/start.sh (or one listening on --port N).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

PORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,2p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

pid="$(running_pid || port_pid "${PORT:-$(default_port)}" || true)"
if [[ -z "$pid" ]]; then
  echo "Autobench is not running."
  rm -f "$PID_FILE"
  exit 0
fi

kill -TERM "$pid" 2>/dev/null || true
# uvicorn waits for open browser WebSockets to close; give it a few seconds, then force it
for _ in $(seq 1 10); do
  ps -p "$pid" >/dev/null 2>&1 || break
  sleep 0.5
done
if ps -p "$pid" >/dev/null 2>&1; then
  kill -KILL "$pid" 2>/dev/null || true
fi
rm -f "$PID_FILE"
echo "Autobench stopped (pid $pid)."
