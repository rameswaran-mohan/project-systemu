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
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sharing_on.events.models import CaptureEvent, EventAction, EventCategory

logger = logging.getLogger(__name__)

# ── Timing thresholds ────────────────────────────────────────────────────────
# Two interaction events within this window are considered the same physical action
DEDUP_WINDOW = timedelta(milliseconds=500)

# Rapid repeated identical clicks (e.g. "Decrease font size" x30) are collapsed
# into a single event with a repeat count if they happen faster than this
REPEAT_COLLAPSE_WINDOW = timedelta(seconds=2.0)

# v0.9.32 Item 2, Layer 3: trailing *bare* raw-hook MOUSE_CLICK events within
# this window before the stop anchor are treated as the operator's "Stop" click
# and trimmed. The trim is anchored to session.end_time (stamped by the
# live-display poll ~0.5s after the real click, plus IPC lag), NOT the SIGINT
# timestamp — so the window is widened to ~1.5s to reliably absorb that lag and
# still capture the bare stop click. Safe to widen because FIX 2C restricts the
# trim to BARE clicks only (no element_text/element_name/url): an enriched legit
# final click in the window survives. Clicks only — never keystrokes.
STOP_CLICK_TRIM_WINDOW = timedelta(milliseconds=1500)


# ── Noise patterns ───────────────────────────────────────────────────────────
# Application names that are part of sharing_on itself — never interesting
SELF_NOISE_PATTERNS = [
    re.compile(r"sharing_on", re.IGNORECASE),
    re.compile(r"view_latest", re.IGNORECASE),
    re.compile(r"python\.exe.*sharing_on", re.IGNORECASE),
    # v0.9.32 Item 2, Layer 2: systemu's own dashboard browser tab.
    re.compile(r"Systemu Dashboard", re.IGNORECASE),
]

# v0.9.32 Item 2, Layer 2 — exact control labels of systemu's own dashboard.
# CROSS-REFERENCE: these MUST stay in sync with the NiceGUI button text in
# systemu/interface/dashboard.py:463 ("Stop & Analyze") and :466 ("Cancel &
# Trash"). The test test_v0932_recording_self_filter pins these strings.
SELF_NOISE_LABELS = {"Stop & Analyze", "Cancel & Trash"}

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

def unify_events(
    events: List[CaptureEvent],
    stop_ts: Optional[datetime] = None,
) -> List[CaptureEvent]:
    """Full unification pipeline.  Returns a clean, deduplicated event list.

    v0.9.32 Item 2, Layer 3: when `stop_ts` (the moment the recorder received
    the stop signal) is supplied, trailing MOUSE_CLICK interactions in
    [stop_ts - STOP_CLICK_TRIM_WINDOW, stop_ts] are dropped — they are the
    operator clicking systemu's own "Stop & Analyze"/"Cancel & Trash" control,
    which has no text/title in the raw-hook representation. No-op when stop_ts
    is None (backward-compatible).
    """

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

    # 5. Filter self-noise (clicks on the sharing_on CLI/viewer / dashboard)
    interactions = _filter_self_noise(interactions)

    # 5b. Layer 3: trim the trailing stop click (raw-hook representation).
    interactions = _trim_trailing_stop_clicks(interactions, stop_ts)

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
    """Remove events that are interactions with sharing_on's / systemu's own UI.

    v0.9.32 Item 2, Layer 2: in addition to the app/title pattern match, drop
    interactions whose element_text is one of systemu's dashboard control labels
    (SELF_NOISE_LABELS, kept in sync with dashboard.py:463/466). This catches the
    introspector- and web-extension-enriched representations of the stop click
    even when the origin env is absent (extension not installed).
    """
    clean = []
    for ev in events:
        app  = ev.application or ""
        title = ev.window_title or ""
        combined = f"{app} {title}"

        if any(pat.search(combined) for pat in SELF_NOISE_PATTERNS):
            continue

        text = (ev.data.get("element_text") or "").strip()
        if text in SELF_NOISE_LABELS:
            continue

        clean.append(ev)
    return clean


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 3: trailing stop-click timestamp trim
# ═══════════════════════════════════════════════════════════════════════════════

def _is_bare_click(ev: CaptureEvent) -> bool:
    """True if a MOUSE_CLICK carries NO identifying context.

    v0.9.32 FIX 2C: the raw-hook representation of the operator's "Stop" click
    has coords only — no element_text, element_name, or url. An ENRICHED final
    click (introspector/web-extension labels) is a legitimate user action and
    must survive even inside the trim window. The labelled/enriched stop-click
    reps are already removed by Layers 1/2.
    """
    data = ev.data or {}
    return not (
        data.get("element_text")
        or data.get("element_name")
        or data.get("url")
    )


def _trim_trailing_stop_clicks(
    events: List[CaptureEvent],
    stop_ts: Optional[datetime],
) -> List[CaptureEvent]:
    """Drop BARE MOUSE_CLICK interactions inside [stop_ts - window, stop_ts].

    Catches the raw-hook representation of systemu's own "Stop" click, which
    carries no element_text/element_name/url for Layers 1/2 to match. Only
    *bare* MOUSE_CLICKs are trimmed (FIX 2C) — an enriched legit final click in
    the window survives — and only MOUSE_CLICK, never a keystroke.
    """
    if stop_ts is None:
        return events

    # FIX 2B: tolerate cross-version data where stop_ts is tz-naive while events
    # are tz-aware (or vice versa). Assume UTC for any naive side so the
    # comparison never raises "can't compare offset-naive and offset-aware".
    if stop_ts.tzinfo is None:
        stop_ts = stop_ts.replace(tzinfo=timezone.utc)
    lo = stop_ts - STOP_CLICK_TRIM_WINDOW
    kept = []
    for ev in events:
        ev_ts = ev.timestamp
        if ev_ts is not None and ev_ts.tzinfo is None:
            ev_ts = ev_ts.replace(tzinfo=timezone.utc)
        if (
            ev.action == EventAction.MOUSE_CLICK
            and _is_bare_click(ev)
            and lo <= ev_ts <= stop_ts
        ):
            logger.debug("Layer 3: trimming trailing bare stop-click at %s", ev.timestamp)
            continue
        kept.append(ev)
    return kept
