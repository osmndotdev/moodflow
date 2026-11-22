"""Microsoft Clarity tag, injected into the served page only where MOODFLOW_CLARITY_ID is set.

Only the VPS `.env` sets the ID, so local dev (Vite on :5173) and local single-port runs collect
nothing. The tag also skips itself on localhost / 127.x / [::1], e.g. an SSH tunnel to the VPS.
"""
from __future__ import annotations

import json
import re

_ID_RE = re.compile(r"^[a-z0-9]{6,32}$")

_TAG = """<script>
  (function (c, l, a, r, i, t, y) {
    if (/^(localhost|127(\\.\\d+){3}|\\[::1\\])$/.test(l.location.hostname)) return;
    c[a] = c[a] || function () { (c[a].q = c[a].q || []).push(arguments); };
    t = l.createElement(r); t.async = 1; t.src = "https://www.clarity.ms/tag/" + i;
    y = l.getElementsByTagName(r)[0]; y.parentNode.insertBefore(t, y);
  })(window, document, "clarity", "script", %s);
</script>
"""


def clarity_id(raw: str | None) -> str | None:
    """Normalized project ID, or None when unset or malformed (it ends up inside a <script>)."""
    value = (raw or "").strip().lower()
    return value if _ID_RE.match(value) else None


def inject_clarity(html: str, project_id: str | None) -> str:
    """Insert the Clarity tag right before </head>; unchanged when there is no ID."""
    if not project_id or "</head>" not in html:
        return html
    return html.replace("</head>", _TAG % json.dumps(project_id) + "  </head>", 1)
