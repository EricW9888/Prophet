#!/usr/bin/env bash
set -euo pipefail

echo "Backend:"
if curl -sf "http://127.0.0.1:8000/health" >/dev/null; then
  echo "  ok http://127.0.0.1:8000/health"
else
  echo "  down http://127.0.0.1:8000/health"
fi

echo "Frontend:"
if curl -sf "http://127.0.0.1:3000" >/dev/null; then
  echo "  ok http://127.0.0.1:3000"
else
  echo "  down http://127.0.0.1:3000"
fi

echo "Ports:"
lsof -iTCP:8000 -sTCP:LISTEN -n -P || true
lsof -iTCP:3000 -sTCP:LISTEN -n -P || true
