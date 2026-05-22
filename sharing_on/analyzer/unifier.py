"""Event Unifier — deduplicates, merges, and cleans raw captured events.

The Omni-Capture engine fires events from multiple independent collectors:
  1. InputHookCollector  — raw mouse clicks with (x, y) but no context.
  2. UIIntrospectCollector — rich Native Desktop labels (Element Name, ControlType).
  3. WebExtensionCollector — rich Web labels (XPath, URL, element_text).

These three sources frequently record the SAME physical user action within
milliseconds of each other.  This module merges them into a single, maximally
enriched timeline before it reaches the StepDetector and LLM.

Pipeline:
    raw events  →  deduplicate  →  filter noise  →  collapse repeats  →  clean events
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Dict, List, Optional

from sharing_on.events.models import CaptureEvent, EventAction, EventCategory

logger = logging.getLogger(__name__)

# ── Timing thresholds ────────────────────────────────────────────────────────
# Two interaction events within this window are considered the same physical action
DEDUP_WINDOW = timedelta(milliseconds=500)

# Rapid repeated identical clicks (e.g. "Decrease font size" x30) are collapsed
# into a single event with a repeat count if they happen faster than this
REPEAT_COLLAPSE_WINDOW = timedelta(seconds=2.0)


# ── Noise patterns ───────────────────────────────────────────────────────────
# Application names that are part of sharing_on itself — never interesting
SELF_NOISE_PATTERNS = [
    re.compile(r"sharing_on", re.IGNORECASE),
    re.compile(r"view_latest", re.IGNORECASE),
    re.compile(r"python\.exe.*sharing_on", re.IGNORECASE),
]

# Generic empty events that add no information
def _is_empty_interaction(event: CaptureEvent) -> bool:
    """Return True if the event is an interaction with no useful payload."""
    if event.category != EventCategory.INTERACTION:
        return False
    data = event.data
    app = event.application or ""
    name = data.get("element_name", "")
    ctrl = data.get("control_type", "")
    tag  = data.get("element_tag", "")

    # Raw hook events: no app, no element
    if (not app or app == "Unknown") and (not name or name == "Unknown") and not tag:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def unify_events(events: List[CaptureEvent]) -> List[CaptureEvent]:
    """Full unification pipeline.  Returns a clean, deduplicated event list."""

    original_count = len(events)

    # 1. Separate interaction events from everything else
    interactions = [e for e in events if e.category == EventCategory.INTERACTION]
    others       = [e for e in events if e.category != EventCategory.INTERACTION]

    # 2. Deduplicate: merge shadow pairs (hook + introspector) into single rich events
    interactions = _deduplicate_shadow_pairs(interactions)

    # 3. Remove empty/useless interaction events that survived dedup
    interactions = [e for e in interactions if not _is_empty_interaction(e)]

    # 4. Collapse rapid repeats (e.g., 30x "Decrease font size" → 1 event with count)
    interactions = _collapse_repeats(interactions)

    # 5. Filter self-noise (clicks on the sharing_on CLI/viewer itself)
    interactions = _filter_self_noise(interactions)

    # 6. Re-merge with non-interaction events and sort by timestamp
    unified = others + interactions
    unified.sort(key=lambda e: e.timestamp)

    logger.info(
        f"Unification: {original_count} raw events → {len(unified)} clean events "
        f"(removed {original_count - len(unified)})"
    )
    return unified


# ═══════════════════════════════════════════════════════════════════════════════
# Deduplication: merge shadow pairs
# ═══════════════════════════════════════════════════════════════════════════════

def _deduplicate_shadow_pairs(events: List[CaptureEvent]) -> List[CaptureEvent]:
    """Merge events that represent the same physical click from different collectors.

    Strategy:  Walk the sorted list.  For each event, look ahead within DEDUP_WINDOW.
    If the next event has a richer payload (more fields filled), merge the rich
    data into the first event and discard the duplicate.
    """
    if len(events) <= 1:
        return events

    events = sorted(events, key=lambda e: e.timestamp)
    merged: List[CaptureEvent] = []
    consumed: set = set()  # indices already merged

    for i, ev in enumerate(events):
        if i in consumed:
            continue

        best = ev
        # Look ahead for duplicates within the time window
        for j in range(i + 1, len(events)):
            if events[j].timestamp - ev.timestamp > DEDUP_WINDOW:
                break
            if j in consumed:
                continue

            candidate = events[j]

            # Same physical action?
            if candidate.action != ev.action:
                continue

            # Merge: pick the richer of the two
            best = _pick_richer(best, candidate)
            consumed.add(j)

        merged.append(best)

    return merged


def _richness_score(event: CaptureEvent) -> int:
    """Score how much useful information an event carries."""
    score = 0
    if event.application and event.application not in ("Unknown", "None"):
        score += 2
    if event.window_title and event.window_title not in ("Unknown", "None"):
        score += 1

    data = event.data
    if data.get("element_name") and data["element_name"] != "Unknown":
        score += 3
    if data.get("control_type") and data["control_type"] != "Unknown":
        score += 2
    if data.get("element_xpath"):
        score += 3
    if data.get("url"):
        score += 2
    if data.get("element_text"):
        score += 2
    if data.get("element_tag"):
        score += 1
    if data.get("value"):
        score += 1
    return score


def _pick_richer(a: CaptureEvent, b: CaptureEvent) -> CaptureEvent:
    """Return a single event that combines the best data from both."""
    score_a = _richness_score(a)
    score_b = _richness_score(b)

    # Start with the richer event as the base
    if score_b > score_a:
        base, donor = b, a
    else:
        base, donor = a, b

    # Back-fill any missing fields from the donor
    if not base.application or base.application in ("Unknown", "None"):
        base.application = donor.application
    if not base.window_title or base.window_title in ("Unknown", "None"):
        base.window_title = donor.window_title

    for key in ("element_name", "control_type", "element_xpath", "url",
                "element_text", "element_tag", "value"):
        if (not base.data.get(key) or base.data[key] in ("Unknown", "")) and donor.data.get(key):
            base.data[key] = donor.data[key]

    return base


# ═══════════════════════════════════════════════════════════════════════════════
# Collapse rapid repeats
# ═══════════════════════════════════════════════════════════════════════════════

def _collapse_repeats(events: List[CaptureEvent]) -> List[CaptureEvent]:
    """Collapse runs of identical actions on the same element into one event.

    E.g., 30 "Decrease font size" clicks within 3 seconds → 1 event with
    data["repeat_count"] = 30.
    """
    if not events:
        return events

    collapsed: List[CaptureEvent] = []
    run_start = events[0]
    run_count = 1

    for i in range(1, len(events)):
        current = events[i]
        same_element = _same_target(run_start, current)
        within_window = (current.timestamp - run_start.timestamp) <= REPEAT_COLLAPSE_WINDOW

        if same_element and within_window:
            run_count += 1
        else:
            # Flush the run
            if run_count > 1:
                run_start.data["repeat_count"] = run_count
            collapsed.append(run_start)
            run_start = current
            run_count = 1

    # Flush last run
    if run_count > 1:
        run_start.data["repeat_count"] = run_count
    collapsed.append(run_start)

    return collapsed


def _same_target(a: CaptureEvent, b: CaptureEvent) -> bool:
    """Do two events describe the same action on the same UI element?"""
    if a.action != b.action:
        return False

    a_name = a.data.get("element_name", "")
    b_name = b.data.get("element_name", "")

    if a_name and b_name and a_name == b_name:
        return True

    a_xpath = a.data.get("element_xpath", "")
    b_xpath = b.data.get("element_xpath", "")

    if a_xpath and b_xpath and a_xpath == b_xpath:
        return True

    return False


# ═══════════════════════════════════════════════════════════════════════════════
# Self-noise filtering
# ═══════════════════════════════════════════════════════════════════════════════

def _filter_self_noise(events: List[CaptureEvent]) -> List[CaptureEvent]:
    """Remove events that are interactions with sharing_on's own UI."""
    clean = []
    for ev in events:
        app  = ev.application or ""
        title = ev.window_title or ""
        combined = f"{app} {title}"

        if any(pat.search(combined) for pat in SELF_NOISE_PATTERNS):
            continue
        clean.append(ev)
    return clean
