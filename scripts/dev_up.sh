#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="$ROOT/backend"
FRONTEND_DIR="$ROOT/frontend"
LOG_DIR="$ROOT/tmp"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

stop_listener() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "$pids" ]]; then
    kill $pids >/dev/null 2>&1 || true
  fi

  for _ in {1..50}; do
    if ! lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      return
    fi
    sleep 0.1
  done

  echo "Port $port did not stop cleanly." >&2
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: scripts/dev_up.sh"
  echo "Starts Prophet in dev mode on 127.0.0.1:3000 and 127.0.0.1:8000."
  exit 0
fi

mkdir -p "$LOG_DIR"
for command in screen curl lsof npm; do
  require_command "$command"
done
if [[ ! -x "$BACKEND_DIR/.venv/bin/python" ]]; then
  echo "Backend environment is missing. Follow README.md setup first." >&2
  exit 1
fi

echo "Starting Prophet backend..."
screen -S prophet-backend -X quit >/dev/null 2>&1 || true
stop_listener 8000
screen -dmS prophet-backend bash -lc \
  "cd '$BACKEND_DIR' && exec .venv/bin/python -m uvicorn investos.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir investos >'$LOG_DIR/backend.log' 2>&1"

echo "Starting Prophet frontend..."
screen -S prophet-frontend -X quit >/dev/null 2>&1 || true
stop_listener 3000
screen -dmS prophet-frontend bash -lc \
  "cd '$FRONTEND_DIR' && exec npm run dev -- --hostname 127.0.0.1 --port 3000 >'$LOG_DIR/frontend.log' 2>&1"

echo "Waiting for services..."
for _ in {1..20}; do
  backend_ok=0
  frontend_ok=0
  curl -sf "http://127.0.0.1:8000/health" >/dev/null && backend_ok=1 || true
  curl -sf "http://127.0.0.1:3000" >/dev/null && frontend_ok=1 || true
  if [[ "$backend_ok" -eq 1 && "$frontend_ok" -eq 1 ]]; then
    echo "Prophet is up."
    exit 0
  fi
  sleep 1
done

echo "Prophet did not come up cleanly."
echo "Backend log: $LOG_DIR/backend.log"
echo "Frontend log: $LOG_DIR/frontend.log"
exit 1
