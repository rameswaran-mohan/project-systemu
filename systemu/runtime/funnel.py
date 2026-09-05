"""P2d - the LOCAL first-run funnel counters.

Six moments in a new operator's first hour, each stamped ONCE with the time it
first happened, into one JSON sidecar in the vault root::

    <vault>/funnel.json   {"welcome_rendered": "2026-...", ...}

WHAT THIS IS NOT
----------------
It is not telemetry. There is no network call here, no batching, no queue, no
identifier of any kind, and nothing that could later be flushed somewhere: the
module's entire I/O surface is ``read_text`` / ``os.replace`` on one path inside
the operator's own vault. The Insights page prints that claim verbatim
(:data:`PRIVACY_NOTE`), and ``tests/test_p2d_funnel.py`` holds the claim to the
source - an AST scan of THIS FILE fails the suite if a network module (or
``subprocess``, or ``webbrowser``) ever appears in its import list. Read that
test as the enforcement of the sentence, because the sentence is the product
promise, not a comment.

It is also not a FACT. Nothing here writes a durable user fact (the
``user_profile`` add path), so this writer owes no R-A16 census entry, and none
of these stamps can reach a planner prompt - pinned by a source scan, which is
also why that function name appears nowhere in this file. It is not an
OnTheTable item either: the reconciler stays the sole writer there.

FIRST-WRITE-WINS, AND WHY THAT IS THE WHOLE DESIGN
-------------------------------------------------
A milestone records when something FIRST happened. Re-stamping on every later
visit would turn "you finished setup on the 3rd" into "you finished setup
today", which is the one thing this table must never say. So :func:`mark_milestone`
reads before it writes and returns :data:`ALREADY_STAMPED` without touching the
file when the key is present. The call sites are all on hot paths (page render,
task submission), so the common case is one small read and no write at all.

CONCURRENCY (DEC-10 / CONC-MAP)
-------------------------------
Unlocked read-modify-write, atomic replace, five caller files (see
``docs/CONC-MAP.md``). Two concurrent runs racing the SAME milestone can lose one
update - and lose nothing, because they were writing the same key and the loser's
value was a few milliseconds later than the winner's. Two racing DIFFERENT
milestones can drop one row; the cost is that the row shows "not yet" until the
next time that milestone's call site runs, which for five of the six is every
task/page. That is why this store gets no lock: the failure mode is a cosmetic
under-count on a read-only table, and a lock here would be ceremony.

NEVER RAISES
------------
Every entry point swallows its own failures and returns a VERDICT string. A
first-run counter that could fail a task submission, a page render or a wish
would be strictly worse than no counter at all.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The one sidecar this module owns, relative to the vault root.
FUNNEL_FILENAME = "funnel.json"

#: The CLOSED vocabulary. A name outside this tuple is refused with a verdict -
#: the store is a fixed six-row table, not a free-form event log, and an
#: open vocabulary is how a counter store quietly becomes telemetry.
MILESTONES: Tuple[str, ...] = (
    "welcome_rendered",
    "setup_finished",
    "first_task_submitted",
    "first_task_succeeded",
    "first_recording",
    "first_wish",
)

#: Operator-facing row labels. Plain English, no jargon, no metric names.
MILESTONE_LABELS: Dict[str, str] = {
    "welcome_rendered":     "Opened the welcome screen",
    "setup_finished":       "Finished setup",
    "first_task_submitted": "Submitted a first task",
    "first_task_succeeded": "First task succeeded",
    "first_recording":      "Started a first recording",
    "first_wish":           "Made a first wish",
}

JOURNEY_TITLE = "Your first-run journey"

#: The privacy claim, rendered verbatim on the Insights page. It is TRUE of this
#: module by construction and pinned by a source-purity test; if either half ever
#: stops being true, change the code, not this sentence.
PRIVACY_NOTE = "Counted on this machine only. Nothing is sent anywhere."

#: What an unstamped row shows.
NOT_YET = "not yet"

#: Shown for a stamp whose timestamp cannot be parsed (a hand-edited file). It
#: happened; we just cannot say when. "not yet" would be the wrong lie.
STAMPED_UNDATED = "recorded"

# --- verdicts (returned, never raised) --------------------------------------
STAMPED = "stamped"                      # newly written
ALREADY_STAMPED = "already_stamped"      # first-write-wins: left untouched
UNKNOWN_MILESTONE = "unknown_milestone"  # not in MILESTONES (or not a str)
NO_VAULT = "no_vault"                    # no usable vault root
WRITE_FAILED = "write_failed"            # the write itself failed


def _funnel_path(vault) -> Optional[Path]:
    """``<vault.root>/funnel.json``, or None when there is no usable root."""
    try:
        root = getattr(vault, "root", None)
        if not root:
            return None
        return Path(root) / FUNNEL_FILENAME
    except Exception:  # noqa: BLE001
        logger.debug("[Funnel] could not resolve the vault root", exc_info=True)
        return None


def read_funnel(vault) -> Dict[str, str]:
    """Every stamped milestone as ``{name: iso_timestamp}``.

    Defensive in every direction: a missing file, an unreadable file, invalid
    JSON, a JSON document that is not an object, an unknown key, or a
    non-string value all read as "not stamped". Rows are filtered rather than
    trusted, so a hand-edited sidecar can never widen the vocabulary the rest
    of this module operates on.

    ``type(x) is T`` (DEC-36) rather than ``isinstance`` on purpose: the values
    here come off disk and a subclass of ``dict``/``str`` arriving from
    ``json.loads`` would mean something has replaced the decoder, which is not a
    thing this reader should quietly accommodate.
    """
    path = _funnel_path(vault)
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        logger.debug("[Funnel] unreadable sidecar at %s", path, exc_info=True)
        return {}
    if type(raw) is not dict:
        logger.debug("[Funnel] sidecar is not a JSON object - ignoring it")
        return {}
    out: Dict[str, str] = {}
    for key in MILESTONES:
        value = raw.get(key)
        if type(value) is str and value:
            out[key] = value
    return out


def _write_atomic(path: Path, data: Dict[str, str]) -> None:
    """tmp + ``os.replace`` in the target directory (CONC-MAP atomic invariant).

    Same-directory temp file so the replace is same-filesystem, hence atomic on
    both POSIX and NT. Raises on failure - :func:`mark_milestone` is the frame
    that turns that into a verdict.
    """
    base = path.parent
    base.mkdir(parents=True, exist_ok=True)
    payload = {name: data[name] for name in MILESTONES if name in data}
    fd, tmp = tempfile.mkstemp(dir=str(base), prefix="funnel.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mark_milestone(vault, name) -> str:
    """Stamp ``name`` with the current UTC time, the FIRST time it happens.

    Returns one of :data:`STAMPED`, :data:`ALREADY_STAMPED`,
    :data:`UNKNOWN_MILESTONE`, :data:`NO_VAULT`, :data:`WRITE_FAILED`.

    Never raises. Every call site is a one-liner on a path that matters far more
    than this counter does.
    """
    if type(name) is not str or name not in MILESTONES:
        logger.debug("[Funnel] refused an unknown milestone name")
        return UNKNOWN_MILESTONE
    path = _funnel_path(vault)
    if path is None:
        return NO_VAULT
    try:
        current = read_funnel(vault)
        if name in current:
            return ALREADY_STAMPED
        current[name] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _write_atomic(path, current)
        return STAMPED
    except Exception:  # noqa: BLE001
        logger.debug("[Funnel] could not stamp %s", name, exc_info=True)
        return WRITE_FAILED


def _render_when(value: str) -> str:
    """An ISO stamp as a bare local-agnostic date, or :data:`STAMPED_UNDATED`."""
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except Exception:  # noqa: BLE001
        return STAMPED_UNDATED


def journey_rows(vault) -> List[Tuple[str, str]]:
    """The Insights table: ``[(label, "YYYY-MM-DD" | "not yet"), ...]``.

    Always exactly one row per milestone, always in :data:`MILESTONES` order, so
    the shape of the table does not change as the operator progresses - only the
    right-hand column does. Read-only and never raises; an unreadable store
    renders as an all-"not yet" table rather than a broken page.
    """
    stamped = read_funnel(vault)
    rows: List[Tuple[str, str]] = []
    for name in MILESTONES:
        value = stamped.get(name)
        rows.append((MILESTONE_LABELS[name],
                     _render_when(value) if value else NOT_YET))
    return rows
