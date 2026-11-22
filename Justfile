# MoodFlow dev commands. `just` with no arguments lists them.

# Interpreter used to create .venv; override with e.g. `just python=python3.14 setup`
python := "python3"

default:
    @just --list

# One-time install (local or server): .venv + Python deps, frontend deps, frontend build, .env
setup:
    @command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || echo "warning: ffmpeg/ffprobe not found on PATH; mixing needs them"
    test -d .venv || {{python}} -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -r requirements.txt
    npm --prefix frontend ci --no-audit --no-fund
    npm --prefix frontend run build
    @test -f .env || (cp .env.example .env && echo "created .env from .env.example; add your API keys there")

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
