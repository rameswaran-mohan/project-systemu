"""Capture-sources filter — keeps/drops events by source app/window in single mode.

v0.9.35 Phase 0: renamed from v0.9.34.1 Feature D's CaptureScope. 'all'
(default) keeps everything system-wide (today's behaviour). 'single' keeps only
events whose application/process/window match one source app (and optionally a
window-title substring). The value tokens were broad/narrow before the rename;
they became all/single so broad/narrow are free for the v0.9.35 generalization
toggle, which is an unrelated record-time control.

Phase-1 attribution rule: events that carry NO app metadata at all (raw global
input-hook clicks/keystrokes, clipboard, full-screen screenshots, SESSION/MARKER
bookkeeping) are KEPT under single mode — they cannot be attributed to an app
without foreground-window correlation, and dropping them would gut a single-app
recording of its interaction stream.
"""

from __future__ import annotations

from dataclasses import dataclass

from sharing_on.events.models import CaptureEvent, EventCategory

# Categories that are pure session bookkeeping and must always survive filtering.
_ALWAYS_KEEP = frozenset({EventCategory.SESSION, EventCategory.MARKER})


@dataclass
class CaptureSources:
    """Decides whether a captured event belongs to the configured source set.

    mode:         "all" (keep all) | "single" (keep only the source app).
    source_app:   process/app name to keep in single mode (case-insensitive,
                  matched against application OR process_name).
    source_title: optional window-title substring to narrow further (one
                  window/tab within the source app).
    """

    mode: str = "all"
    source_app: str = ""
    source_title: str = ""

    @property
    def is_single(self) -> bool:
        """True only when single AND a usable source was given.

        A 'single' mode with an empty source_app degrades to all so a
        misconfigured session never silently drops every event.
        """
        return self.mode == "single" and bool(self.source_app.strip())

    def keep(self, event: CaptureEvent) -> bool:
        """Return True if `event` should be captured under this source set."""
        if not self.is_single:
            return True
        if event.category in _ALWAYS_KEEP:
            return True

        app = (event.application or "").lower()
        proc = (event.process_name or "").lower()
        # Events with NO app metadata (raw input-hook, clipboard, full-screen
        # screenshots) can't be attributed to an app — keep them rather than
        # gut the single recording's interaction stream.
        if not app and not proc:
            return True
        # The interaction stream can't be reliably attributed to an app in
        # Phase 1: the Windows UI introspector emits the RICHEST clicks but
        # stamps `application` with the window TITLE (not a process) and no
        # process_name. Keep interaction events that lack a process_name rather
        # than drop the best signal. (Web-extension interaction events never
        # reach here — that collector overrides should_capture + filters by
        # origin.)
        if event.category == EventCategory.INTERACTION and not proc:
            return True

        # Normalise a Windows-style ".exe" source so it also matches friendly
        # app names ("Google Chrome" on macOS) and window titles cross-platform.
        target = self.source_app.strip().lower()
        if target.endswith(".exe"):
            target = target[:-4]
        if target not in app and target not in proc:
            return False

        title_needle = self.source_title.strip().lower()
        if title_needle:
            return title_needle in (event.window_title or "").lower()
        return True
