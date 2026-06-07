"""Strip ANSI escape sequences from CLI tool output."""
from __future__ import annotations

import re

# Matches CSI escape sequences (most colors, cursor moves, screen clears).
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    """Return ``text`` with all ANSI escape sequences removed."""
    if not text:
        return text
    return _ANSI_RE.sub("", text)
