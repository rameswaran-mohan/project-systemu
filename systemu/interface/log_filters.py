"""Logging filters for noisy-but-benign NiceGUI runtime records (W3.1).

The dashboard's liveness timers (``ui.timer``) can fire one tick *after* their
page/slot was disposed on navigation, raising
``RuntimeError('The parent slot of the element has been deleted.')``. NiceGUI
already drops the tick — the app is unaffected — but logs the full traceback
via ``logging.getLogger('nicegui').exception(...)`` on essentially every
navigation, flooding the logs (and, under rapid navigation, stalling the
renderer with the volume). ``safe_timer`` can't catch it: the error is raised
in NiceGUI's pre-callback context entry, before our callback runs.

This installs a precise filter that suppresses ONLY that record. Every other
NiceGUI error still logs normally.
"""
from __future__ import annotations

import logging

_NEEDLE = "parent slot of the element has been deleted"


class DropParentSlotDeleted(logging.Filter):
    """Drop the benign post-navigation 'parent slot deleted' timer record."""

    def filter(self, record: logging.LogRecord) -> bool:  # True = keep
        try:
            msg = str(record.getMessage())
            exc = record.exc_info[1] if record.exc_info else None
            blob = (msg + " " + (str(exc) if exc else "")).lower()
            return _NEEDLE not in blob
        except Exception:
            return True  # never drop a record because the filter itself errored


def install_nicegui_log_filters() -> None:
    """Attach the benign-error filter to the ``nicegui`` logger (idempotent)."""
    lg = logging.getLogger("nicegui")
    if not any(isinstance(f, DropParentSlotDeleted) for f in lg.filters):
        lg.addFilter(DropParentSlotDeleted())
