# Shared by start.sh / stop.sh / status.sh.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
WEB="$ROOT/web"
VENV="$BACKEND/.venv"
RUN_DIR="$BACKEND/data"
PID_FILE="$RUN_DIR/autobench.pid"
LOG_FILE="$RUN_DIR/autobench.log"

# Port: --port flag (parsed by the caller) > $AUTOBENCH_PORT > AUTOBENCH_PORT in .env > 8000
default_port() {
  if [[ -n "${AUTOBENCH_PORT:-}" ]]; then echo "$AUTOBENCH_PORT"; return; fi
  local p=""
  [[ -f "$ROOT/.env" ]] && p="$(grep -E '^AUTOBENCH_PORT=' "$ROOT/.env" | tail -1 | cut -d= -f2 | tr -d ' "'"'"'')"
  echo "${p:-8000}"
}

find_dart() {
  if command -v dart >/dev/null 2>&1; then command -v dart
  elif [[ -x /usr/local/bin/flutter/bin/dart ]]; then echo /usr/local/bin/flutter/bin/dart
  else return 1; fi
}

# PID of the running server, from the pid file (only if that process is still an autobench server)
running_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid; pid="$(cat "$PID_FILE")"
  if [[ -n "$pid" ]] && ps -p "$pid" -o command= 2>/dev/null | grep -q "autobench"; then echo "$pid"; return 0; fi
  rm -f "$PID_FILE"
  return 1
}

# PID of an autobench server listening on a port (e.g. one started by hand)
port_pid() {
  local pid
  for pid in $(lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null); do
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "autobench"; then echo "$pid"; return 0; fi
  done
  return 1
}
