#!/usr/bin/env bash
# Show whether Autobench is running, and where.
set -uo pipefail
# resolve symlinks (e.g. ~/.local/bin/autobench-start) to find the repo
SELF="${BASH_SOURCE[0]}"
while [[ -L "$SELF" ]]; do t="$(readlink "$SELF")"; [[ "$t" == /* ]] && SELF="$t" || SELF="$(dirname "$SELF")/$t"; done
source "$(dirname "$SELF")/_common.sh"

PORT="${1:-}"
[[ "$PORT" == "--port" ]] && PORT="${2:-}"
pid="$(running_pid || port_pid "${PORT:-$(default_port)}")" || { echo "Autobench is not running."; exit 1; }
port="$(lsof -Pan -p "$pid" -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {sub(/.*:/, "", $9); print $9; exit}')"
echo "Autobench is running (pid $pid) on http://127.0.0.1:${port:-?}"
[[ -n "$port" ]] && curl -fs "http://127.0.0.1:$port/api/health" && echo
echo "Logs: $LOG_FILE"
