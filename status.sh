#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")"

PID_FILE=".mcp-server.pid"
LOG_FILE=".mcp-server.log"
PORT="${WORKSPACE_MCP_PORT:-8000}"
HOST="${WORKSPACE_MCP_HOST:-localhost}"
[[ "$HOST" == "0.0.0.0" ]] && HOST="localhost"
URL="http://${HOST}:${PORT}/mcp"

pid=""
[[ -f "$PID_FILE" ]] && pid="$(cat "$PID_FILE" 2>/dev/null || true)"

port_pids="$(lsof -ti:"$PORT" 2>/dev/null || true)"

pid_alive=0
if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
  pid_alive=1
fi

# Recorded PID is alive and bound to the port → healthy.
if (( pid_alive )) && [[ -n "$port_pids" ]] && grep -qx "$pid" <<<"$port_pids"; then
  echo "MCP server: RUNNING"
  echo "  PID:  $pid"
  echo "  Port: $PORT"
  echo "  URL:  $URL"
  echo "  Logs: $LOG_FILE"

  # Probe the HTTP endpoint if curl is available. POST / returns 405, /mcp speaks JSON-RPC
  # and rejects empty bodies — either way a TCP response means the server is accepting.
  if command -v curl >/dev/null 2>&1; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "$URL" || echo "000")"
    if [[ "$code" == "000" ]]; then
      echo "  HTTP: no response (server may still be starting)"
    else
      echo "  HTTP: $code"
    fi
  fi
  exit 0
fi

# Recorded PID is dead but the file is still there.
if [[ -n "$pid" ]] && (( ! pid_alive )); then
  echo "MCP server: STOPPED (stale PID file — recorded PID $pid is dead)"
  if [[ -n "$port_pids" ]]; then
    echo "  Warning: port $PORT is in use by another process: $port_pids"
  fi
  echo "  Run ./start.sh to start, or rm $PID_FILE to clear the stale record."
  exit 1
fi

# No PID file but something else is on the port.
if [[ -z "$pid" ]] && [[ -n "$port_pids" ]]; then
  echo "MCP server: NOT TRACKED (port $PORT is bound by PID(s) $port_pids, but no $PID_FILE)"
  echo "  Likely started outside ./start.sh. Use ./restart.sh to take ownership cleanly."
  exit 2
fi

echo "MCP server: STOPPED"
echo "  Run ./start.sh to start it."
exit 1
