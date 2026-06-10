"""Single source of truth for the *needs-consolidation* decision.

Before this module three surfaces disagreed on when a shadow's buffered
lessons were "ready to consolidate":

  • ``interface/components/memory_status.py`` flagged "pending" at >= 5 raw
    file lines of ``memory_buffer.jsonl`` (a hardcoded literal, counting raw
    lines rather than parsed entries).
  • ``interface/pages/memory_consolidation_page.py`` used
    ``(len(buf) >= BUFFER_THRESHOLD or is_stale) and bool(buf)`` with parsed
    entries.
  • the engine (``scheduler.jobs.run_consolidation_for_all``) gated on
    ``len(buffer_entries) >= BUFFER_THRESHOLD or is_stale``.

``needs_consolidation`` collapses these onto ONE rule, reading the
canonical ``BUFFER_THRESHOLD`` / ``STALE_AFTER_DAYS`` constants from
``scheduler.jobs`` so the dashboard can never drift from the engine again.
It operates on *parsed* inputs — the buffer entry list and the
SHADOW_MEMORY.md text returned by ``vault.load_shadow_memory(sid)`` — never
on raw file lines.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Sequence

from systemu.core.utils import utcnow

# Sentinel: a shadow with no parseable ``last_consolidated:`` frontmatter is
# treated as having been consolidated a year ago, which makes it stale-eligible
# (subject to the trailing ``bool(buf)`` guard).  This mirrors the fallback in
# ``memory_consolidation_page._parse_last_consolidated`` exactly.
_FALLBACK_DAYS = 365


def _parse_last_consolidated(md_text: str) -> datetime:
    """Read the ``last_consolidated:`` frontmatter; fall back to ~1 year ago.

    Replicates ``memory_consolidation_page._parse_last_consolidated`` so the
    staleness clock is identical across every surface.
    """
    fallback = utcnow() - timedelta(days=_FALLBACK_DAYS)
    if not md_text:
        return fallback
    m = re.search(r"^last_consolidated:\s*(.+)$", md_text, re.MULTILINE)
    if not m:
        return fallback
    try:
        return datetime.fromisoformat(m.group(1).strip().replace("Z", ""))
    except ValueError:
        return fallback


def _is_stale(md_text: str) -> bool:
    """True if the time since last consolidation exceeds STALE_AFTER_DAYS."""
    from systemu.scheduler.jobs import STALE_AFTER_DAYS

    last_dt = _parse_last_consolidated(md_text)
    return (utcnow() - last_dt) > timedelta(days=STALE_AFTER_DAYS)


def needs_consolidation(buf: Sequence, md_text: str) -> bool:
    """The single needs-consolidation rule shared by every surface.

    Triggers when the parsed buffer has reached the threshold OR the memory is
    stale — but only ever when there is something buffered to fold in.  This is
    exactly ``memory_consolidation_page``'s prior expression, now centralised::

        (len(buf) >= BUFFER_THRESHOLD or _is_stale(md_text)) and bool(buf)

    ``buf`` is the *parsed* buffer-entry list (e.g. the second element of
    ``vault.load_shadow_memory(sid)``), never a raw line count.
    """
    from systemu.scheduler.jobs import BUFFER_THRESHOLD

    has_buf = len(buf) >= BUFFER_THRESHOLD
    return (has_buf or _is_stale(md_text)) and bool(buf)
