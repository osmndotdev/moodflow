# MoodFlow dev commands. `just` with no arguments lists them.

# Interpreter used to create .venv; override with e.g. `just python=python3.14 setup`
python := "python3"

default:
    @just --list

# One-time install (local or server): .venv + Python deps, frontend deps, frontend build, .env
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    py="{{python}}"
    hint="On Debian/Ubuntu: sudo apt install python3-venv (or python3.14-venv for a versioned Python), or install uv."
    if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
        echo "warning: ffmpeg/ffprobe not found on PATH; mixing needs them (Debian/Ubuntu: sudo apt install ffmpeg)"
    fi
    if command -v uv >/dev/null; then
        # uv needs neither pip nor the venv/ensurepip package
        [ -x .venv/bin/python ] || uv venv --quiet --python "$py" .venv
        uv pip install --quiet --python .venv/bin/python -r requirements.txt
    else
        if [ ! -x .venv/bin/python ]; then
            # Debian's python3 without python3-venv fails here but still leaves a pip-less .venv behind
            "$py" -m venv .venv || true
        fi
        if [ ! -x .venv/bin/python ]; then
            echo "error: could not create .venv with $py. $hint" >&2
            exit 1
        fi
        if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
            echo ".venv has no pip; installing it with ensurepip"
            if ! .venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1; then
                echo "error: .venv has no pip and ensurepip is unavailable. $hint Then re-run: just setup" >&2
                exit 1
            fi
        fi
        .venv/bin/python -m pip install --quiet --upgrade pip
        .venv/bin/python -m pip install --quiet -r requirements.txt
    fi
    npm --prefix frontend ci --no-audit --no-fund
    npm --prefix frontend run build
    if [ ! -f .env ]; then
        cp .env.example .env
        echo "created .env from .env.example; add your API keys there"
    fi

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
