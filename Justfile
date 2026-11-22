# MoodFlow dev commands. `just` with no arguments lists them.

default:
    @just --list

# Backend + frontend in one terminal, both auto-reloading (Ctrl-C stops both)
dev:
    ./dev.sh

# Backend only (reloads on backend/*.py edits; host/port from .env)
backend:
    .venv/bin/python -m backend

# Frontend only (Vite hot reload, proxies /api to the backend)
frontend:
    npm --prefix frontend run dev

# Run the offline test suite
test *args:
    .venv/bin/python -m pytest -q {{args}}
