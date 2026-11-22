#!/usr/bin/env bash
# Dev mode: backend + frontend in one terminal, both auto-reloading. Ctrl-C stops both.
#   backend  → MOODFLOW_HOST:MOODFLOW_PORT from .env (default 127.0.0.1:8000), reloads on backend/*.py edits
#   frontend → http://localhost:5173, Vite hot reload, proxies /api to the backend
set -uo pipefail
cd "$(dirname "$0")"

pids=()
cleanup() {
  trap - INT TERM EXIT
  if [ "${#pids[@]}" -gt 0 ]; then
    kill "${pids[@]}" 2>/dev/null  # uvicorn's reloader also stops its worker on SIGTERM
  fi
  wait 2>/dev/null
}
trap cleanup INT TERM EXIT

.venv/bin/python -m backend &
pids+=($!)
# vite directly (not via npm) so the signal reaches the server process itself
(cd frontend && exec node_modules/.bin/vite) &
pids+=($!)

wait
