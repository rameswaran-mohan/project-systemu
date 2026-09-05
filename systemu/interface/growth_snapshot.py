"""2a - the weekly growth snapshot behind Home's "This week: +N tools" line.

The Home growth card's odometer says what the install HOLDS. To say what it
GAINED, something has to remember last week - so this module owns exactly one
durable thing: a single JSON sidecar at ``<vault root>/growth_snapshot.json``
holding the last stamped ``growth_counts`` and the aware-UTC time it was taken.

    {"counts": {"tools": 3, "shadows": 1, "skills": 2},
     "iso_ts": "2026-08-14T12:00:00+00:00"}

Written from the HOME RENDER path (``pages/console._build_growth_card``), at
most once per ``SNAPSHOT_INTERVAL_DAYS``, and never anywhere else - see the
CONC-MAP row and the writer-ownership entry that pin that (docs/CONC-MAP.md,
tests/test_conc_map_writer_ownership.py). It is NOT a user fact and NOT an
OnTheTable item: the reconciler keeps its sole-writer invariant.

Two disciplines this store inherits from the other vault sidecars:

  * ATOMIC WRITES - tempfile + ``os.replace`` - so a crash mid-write can never
    leave a torn file for the next render to choke on.
  * DEFENSIVE READS - a missing, corrupt, or wrong-shaped file is simply "no
    snapshot". The cost of a broken file is one silent week, never a broken
    Home page; the next render re-stamps it.

HONEST SILENCE is the copy rule: the line renders only when a prior snapshot
exists AND some counter went up. No prior, no growth, or a SHRUNKEN install all
render nothing at all - a "+0", or a delta measured against a week that was
never recorded, would be a claim the store cannot back.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

#: The one file this module owns, directly under the vault root.
SNAPSHOT_FILENAME = "growth_snapshot.json"

#: How old a stored snapshot may get before the next render re-stamps it.
SNAPSHOT_INTERVAL_DAYS = 7

#: Render order + (singular, plural) label for each counter. Same three keys
#: ``console.growth_counts`` returns; anything else on disk is ignored.
_COUNTERS = (
    ("tools",   "tool",   "tools"),
    ("shadows", "shadow", "shadows"),
    ("skills",  "skill",  "skills"),
)


def _count(raw: Any) -> int:
    """A counter value off disk as an int - anything else reads as 0.

    ``type(x) is int`` (DEC-36) rather than ``isinstance``: it is the only check
    that does not dispatch, and it excludes ``bool`` for free, so a stored
    ``true`` can never be counted as one tool.
    """
    return raw if type(raw) is int else 0


def snapshot_path(vault) -> Optional[Path]:
    """``<vault root>/growth_snapshot.json``, or None if there is no root.

    A vault stand-in without a usable ``root`` yields None rather than raising,
    so every caller below degrades to "no snapshot" instead of to a traceback.
    """
    root = getattr(vault, "root", None)
    if root is None:
        return None
    try:
        text = str(root).strip()
        if not text:
            return None
        return Path(text) / SNAPSHOT_FILENAME
    except Exception:
        return None


def load_snapshot(vault) -> Optional[Dict[str, Any]]:
    """The stored snapshot, or None for absent / unreadable / wrong-shaped.

    The shape check is part of the defence: a file that parses but is not
    ``{"counts": {...}, "iso_ts": ...}`` is no more usable than one that does
    not parse, and treating it as a snapshot would produce a delta against
    junk.
    """
    try:
        path = snapshot_path(vault)
        if path is None or not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if type(raw) is not dict:
            return None
        if type(raw.get("counts")) is not dict:
            return None
        return raw
    except Exception:
        return None


def snapshot_due(snapshot: Any, now: datetime) -> bool:
    """Should this render stamp a new weekly snapshot?

    True when there is no snapshot, when its timestamp cannot be read as an
    AWARE datetime, or when it is at least ``SNAPSHOT_INTERVAL_DAYS`` old.
    A stamp in the FUTURE (a clock that moved backwards) is also due: the
    alternative is a sidecar frozen until the stored future arrives.
    """
    if type(snapshot) is not dict:
        return True
    raw = snapshot.get("iso_ts")
    if type(raw) is not str:
        return True
    try:
        stamped = datetime.fromisoformat(raw)
    except Exception:
        return True
    if stamped.tzinfo is None:
        return True
    try:
        age = now - stamped
    except Exception:
        return True
    return not (timedelta(0) <= age < timedelta(days=SNAPSHOT_INTERVAL_DAYS))


def compute_delta(prior_counts: Any, current_counts: Any) -> Dict[str, int]:
    """``current - prior`` per counter (pure). No prior -> all zeros.

    NO PRIOR IS NOT A PRIOR OF ZERO. A missing (or unreadable) snapshot yields a
    zero delta, never "everything you have, gained this week" - the store cannot
    back that sentence, and this is the single place that rule lives, so every
    "no snapshot yet" path above inherits honest silence for free.

    Negatives are returned as negatives rather than clamped: the caller decides
    what to SAY, and a store that quietly reported a shrink as 0 would be
    hiding the one case the line must stay silent about.
    """
    if type(prior_counts) is not dict:
        return {key: 0 for key, _sing, _plur in _COUNTERS}
    prior = prior_counts
    current = current_counts if type(current_counts) is dict else {}
    return {key: _count(current.get(key)) - _count(prior.get(key))
            for key, _sing, _plur in _COUNTERS}


def delta_line(delta: Any) -> str:
    """"This week: +2 tools, +1 shadow" - or "" when there is nothing to say.

    Only POSITIVE terms render, in odometer order. All-zero (or all-negative)
    yields the empty string, and the card omits the line entirely.
    """
    rows = delta if type(delta) is dict else {}
    parts = []
    for key, singular, plural in _COUNTERS:
        n = _count(rows.get(key))
        if n > 0:
            parts.append("+%d %s" % (n, singular if n == 1 else plural))
    return "This week: " + ", ".join(parts) if parts else ""


def _write_growth_snapshot(path: Path, payload: Dict[str, Any]) -> None:
    """Atomic write (tempfile + os.replace) - the vault side-store convention.

    Private on purpose: ``growth_delta_line`` is the only way in, so the CONC-MAP
    writer set stays a set of ONE. ``tests/test_home_growth_delta.py::
    test_the_private_writer_has_no_caller_outside_its_module`` pins that.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def growth_delta_line(vault, counts: Any, *, now: Optional[datetime] = None) -> str:
    """The Home render-path entry point: the week's delta line, and the stamp.

    Reads the prior snapshot, computes the delta against it FIRST, then - only
    if the stored week has rolled over - overwrites it with today's counts. That
    order is what lets the roll-over render still tell the operator what the week
    produced instead of resetting to silence.

    Never raises: any failure (no root, unwritable vault, corrupt file) costs at
    most this one line. The write is a side effect of rendering, so it can never
    be worth a broken Home page.
    """
    try:
        if type(now) is not datetime or now.tzinfo is None:
            now = datetime.now(timezone.utc)
        current = counts if type(counts) is dict else {}
        prior = load_snapshot(vault)
        prior_counts = prior.get("counts") if type(prior) is dict else None
        line = delta_line(compute_delta(prior_counts, current))
        if snapshot_due(prior, now):
            path = snapshot_path(vault)
            if path is not None:
                _write_growth_snapshot(path, {
                    "counts": {key: _count(current.get(key))
                               for key, _s, _p in _COUNTERS},
                    "iso_ts": now.isoformat(),
                })
        return line
    except Exception:
        return ""
