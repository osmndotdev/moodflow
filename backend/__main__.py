"""Run the API with host/port from .env: `.venv/bin/python -m backend`.

Auto-reloads on edits to backend/*.py (set MOODFLOW_RELOAD=0 to turn it off).
Only backend/ is watched: tmp/ gets audio and cache writes during a mix, and a
reload would drop the in-memory mix jobs. Edits to .env need a manual restart.
"""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
load_dotenv(BACKEND_DIR.parent / ".env")

if __name__ == "__main__":
    reload = os.getenv("MOODFLOW_RELOAD", "1").strip().lower() not in ("0", "false", "no", "")
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("MOODFLOW_HOST", "127.0.0.1"),
        port=int(os.getenv("MOODFLOW_PORT", 8000)),
        reload=reload,
        reload_dirs=[str(BACKEND_DIR)] if reload else None,
    )
