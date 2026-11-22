"""Run the API with host/port from .env: `.venv/bin/python -m backend`."""
from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("MOODFLOW_HOST", "127.0.0.1"),
        port=int(os.getenv("MOODFLOW_PORT", 8000)),
    )
