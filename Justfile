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
    cksum < requirements.txt > .venv/.requirements.cksum  # lets `just update` skip unchanged deps
    npm --prefix frontend ci --no-audit --no-fund
    cksum < frontend/package-lock.json > frontend/node_modules/.package-lock.cksum
    npm --prefix frontend run build
    if [ ! -f .env ]; then
        cp .env.example .env
        echo "created .env from .env.example; add your API keys there"
    fi

# After pushing a new version (run where it's deployed): pull, reinstall only changed deps, upgrade yt-dlp, rebuild, restart
update:
    git pull --ff-only
    @# a fresh `just` so a Justfile changed by the pull is the one that runs
    @just _refresh

_refresh:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -x .venv/bin/python ] || [ ! -d frontend/node_modules ]; then
        echo "error: no .venv or frontend/node_modules yet; run: just setup" >&2
        exit 1
    fi
    pip_install() {
        if command -v uv >/dev/null; then
            uv pip install --quiet --python .venv/bin/python "$@"
        else
            .venv/bin/python -m pip install --quiet "$@"
        fi
    }
    if [ "$(cksum < requirements.txt)" != "$(cat .venv/.requirements.cksum 2>/dev/null)" ]; then
        echo "requirements.txt changed; installing Python dependencies"
        pip_install -r requirements.txt
        cksum < requirements.txt > .venv/.requirements.cksum
    fi
    # YouTube changes keep breaking older yt-dlp releases, so keep it current on every update
    old=$(.venv/bin/python -m yt_dlp --version 2>/dev/null || echo none)
    pip_install --upgrade "yt-dlp[default]"
    new=$(.venv/bin/python -m yt_dlp --version)
    [ "$old" = "$new" ] || echo "yt-dlp $old -> $new"
    if [ "$(cksum < frontend/package-lock.json)" != "$(cat frontend/node_modules/.package-lock.cksum 2>/dev/null)" ]; then
        echo "package-lock.json changed; installing frontend dependencies"
        npm --prefix frontend ci --no-audit --no-fund
        cksum < frontend/package-lock.json > frontend/node_modules/.package-lock.cksum
    fi
    npm --prefix frontend run build
    keys() { grep -oE '^[A-Z_][A-Z0-9_]*=' "$1" 2>/dev/null | tr -d '=' | sort -u || true; }
    missing=$(comm -23 <(keys .env.example) <(keys .env) | tr '\n' ' ')
    if [ -n "$missing" ]; then
        echo "note: .env lacks settings that .env.example has (their defaults apply): $missing"
    fi
    restart=$(grep -E '^MOODFLOW_RESTART_CMD=' .env 2>/dev/null | tail -1 | cut -d= -f2- || true)
    restart=${restart#[\"\']}
    restart=${restart%[\"\']}
    if [ -n "$restart" ]; then
        echo "restarting: $restart"
        bash -c "$restart"
    else
        echo "updated; now restart the backend (set MOODFLOW_RESTART_CMD in .env to have this done for you)"
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
