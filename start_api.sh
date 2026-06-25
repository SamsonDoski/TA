#!/usr/bin/env bash
# One-command local launcher for the Quant Desk API.
# Uses the repo venv if present, otherwise the active python.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

if [ -x "venv/bin/python" ]; then
  PY="venv/bin/python"
else
  PY="python3"
fi

echo "Starting Quant Desk API on http://localhost:${PORT}  (docs: /docs)"
exec "$PY" -m uvicorn api.server:app --host 0.0.0.0 --port "$PORT" --reload
