"""Capture-scope filter — keeps/drops events by target app/window in narrow mode.

v0.9.34.1 Feature D. 'broad' (default) keeps everything system-wide (today's
behaviour). 'narrow' keeps only events whose application/process/window match a
single target app (and optionally a window-title substring).

Phase-1 attribution rule: events that carry NO app metadata at all (raw global
input-hook clicks/keystrokes, clipboard, full-screen screenshots, SESSION/MARKER
bookkeeping) are KEPT under narrow mode — they cannot be attributed to an app
without foreground-window correlation, and dropping them would gut a narrow
recording of its interaction stream. Narrow Phase 1 therefore strips the obvious
cross-app noise (other windows/processes/browser origins) while preserving the
ambient signal; Phase 2 will correlate input events to the foreground window for
true per-app input narrowing.
"""

from __future__ import annotations

from dataclasses import dataclass

from sharing_on.events.models import CaptureEvent, EventCategory

# Categories that are pure session bookkeeping and must always survive narrowing.
_ALWAYS_KEEP = frozenset({EventCategory.SESSION, EventCategory.MARKER})


@dataclass
class CaptureScope:
    """Decides whether a captured event belongs to the configured scope.

    scope:        "broad" (keep all) | "narrow" (keep only the target app).
    target_app:   process/app name to keep in narrow mode (case-insensitive,
                  matched against application OR process_name).
    target_title: optional window-title substring to narrow further (one
                  window/tab within the target app).
    """

    scope: str = "broad"
    target_app: str = ""
    target_title: str = ""

    @property
    def is_narrow(self) -> bool:
        """True only when narrow AND a usable target was given.

        A 'narrow' scope with an empty target_app degrades to broad so a
        misconfigured session never silently drops every event.
        """
        return self.scope == "narrow" and bool(self.target_app.strip())

    def keep(self, event: CaptureEvent) -> bool:
        """Return True if `event` should be captured under this scope."""
        if not self.is_narrow:
            return True
        if event.category in _ALWAYS_KEEP:
            return True

        app = (event.application or "").lower()
        proc = (event.process_name or "").lower()
        # Phase-1: events with NO app metadata (raw input-hook, clipboard,
        # full-screen screenshots) can't be attributed to an app — keep them
        # rather than gut the narrow recording's interaction stream.
        if not app and not proc:
            return True
        # The interaction stream can't be reliably attributed to an app in
        # Phase 1 either: the Windows UI introspector emits the RICHEST clicks
        # but stamps `application` with the window TITLE (not a process) and no
        # process_name. Keep interaction events that lack a process_name rather
        # than drop the best signal; Phase 2 will correlate input to the
        # foreground window and (once it sets process_name) the match below
        # narrows them properly. (Web-extension interaction events never reach
        # here — that collector overrides should_capture + filters by origin.)
        if event.category == EventCategory.INTERACTION and not proc:
            return True

        # Normalise a Windows-style ".exe" target so it also matches friendly
        # app names ("Google Chrome" on macOS) and window titles cross-platform.
        target = self.target_app.strip().lower()
        if target.endswith(".exe"):
            target = target[:-4]
        if target not in app and target not in proc:
            return False

        title_needle = self.target_title.strip().lower()
        if title_needle:
            return title_needle in (event.window_title or "").lower()
        return True
