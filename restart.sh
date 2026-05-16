#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE=".mcp-server.pid"
PORT="${WORKSPACE_MCP_PORT:-8000}"

stop_pid() {
  local pid="$1"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in {1..20}; do
      kill -0 "$pid" 2>/dev/null || return 0
      sleep 0.25
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
}

if [[ -f "$PID_FILE" ]]; then
  stop_pid "$(cat "$PID_FILE")"
  rm -f "$PID_FILE"
fi

# Fallback: anything still bound to the port (e.g. orphaned from a prior run)
if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "Killing leftover process(es) on port $PORT"
  lsof -ti:"$PORT" | xargs kill 2>/dev/null || true
  sleep 1
  lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
fi

exec ./start.sh
