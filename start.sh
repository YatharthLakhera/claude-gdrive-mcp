#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE=".mcp-server.pid"
LOG_FILE=".mcp-server.log"
PORT="${WORKSPACE_MCP_PORT:-8000}"

if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "Server already running (PID $(cat "$PID_FILE")). Use ./restart.sh to restart."
  exit 1
fi

if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "Port $PORT is already in use by another process. Free it or change WORKSPACE_MCP_PORT."
  exit 1
fi

nohup uv run main.py --transport streamable-http >"$LOG_FILE" 2>&1 &
PID=$!
echo "$PID" > "$PID_FILE"

# Verify the server actually came up: process must still be alive AND bound to the port.
for _ in {1..20}; do
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  if lsof -ti:"$PORT" >/dev/null 2>&1; then
    echo "Started MCP server (PID $PID)"
    echo "  URL:  http://localhost:$PORT/mcp"
    echo "  Logs: $LOG_FILE"
    exit 0
  fi
  sleep 0.25
done

echo "MCP server failed to start. Last log lines:" >&2
tail -n 30 "$LOG_FILE" >&2
rm -f "$PID_FILE"
exit 1
