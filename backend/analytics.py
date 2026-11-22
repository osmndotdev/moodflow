"""Microsoft Clarity config, injected into the served page only where MOODFLOW_CLARITY_ID is set.

Only the VPS `.env` sets the ID, so local dev (Vite on :5173) and local single-port runs collect
nothing. The page only gets the ID here: the frontend (`src/consent.ts`) shows a cookie banner and
loads the Clarity tag after the visitor accepts, never before. It also stays off on localhost /
127.x / [::1], e.g. an SSH tunnel to the VPS.
"""
from __future__ import annotations

import json
import re

_ID_RE = re.compile(r"^[a-z0-9]{6,32}$")


def clarity_id(raw: str | None) -> str | None:
    """Normalized project ID, or None when unset or malformed (it ends up inside a <script>)."""
    value = (raw or "").strip().lower()
    return value if _ID_RE.match(value) else None


def inject_clarity(html: str, project_id: str | None) -> str:
    """Expose the project ID to the page right before </head>; unchanged when there is no ID."""
    if not project_id or "</head>" not in html:
        return html
    tag = f"<script>window.MOODFLOW_CLARITY_ID = {json.dumps(project_id)};</script>\n"
    return html.replace("</head>", tag + "  </head>", 1)
