"""Single-Python-interpreter invariant for daemon + worker(s).

The runtime depends on tool pip-installs landing in the same interpreter
that imports and runs the tool.  When the daemon is launched from one
Python (e.g. ``.venv\\Scripts\\python.exe``) and the worker from another
(e.g. system ``python.exe``), an install triggered by the daemon never
becomes visible to the worker — exactly the failure mode that surfaced
during v0.3 e2e validation of ``create_word_doc`` / ``python-docx``.

This module is the cheapest possible defence: every long-lived runtime
process records its ``sys.executable`` in a shared file on boot, and
later starters compare against the recorded value.  On mismatch we log
a high-visibility warning and surface an Event Log entry, but do NOT
hard-fail — operators already running this way would otherwise lose
service mid-flight.  A future v0.3.5 may upgrade to hard-fail once the
launchers consistently reuse the daemon's interpreter.

Lock file format (``<data_dir>/runtime.lock`` JSON):

    {
        "version":      1,
        "interpreter":  "C:\\path\\to\\python.exe",
        "recorded_by":  "daemon",        # or "worker"
        "recorded_pid": 40616,
        "recorded_at":  "2026-05-13T..."
    }

The lock file is informational; we don't enforce mutual exclusion via it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LOCK_FILENAME = "runtime.lock"


@dataclass
class InterpreterCheckResult:
    matches:              bool
    expected_interpreter: Optional[str]   # what was on file, if anything
    actual_interpreter:   str
    recorded_by:          Optional[str]   # "daemon" / "worker" / None
    note:                 str             # human-readable summary


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def _lock_path(data_dir: Path) -> Path:
    return Path(data_dir) / _LOCK_FILENAME


def record_interpreter(data_dir: Path, *, recorded_by: str) -> None:
    """Record the current process's interpreter as the canonical one.

    Called by the daemon at startup.  Overwrites any stale lock from a
    prior run (we assume process IDs do not persist across daemon
    restarts — if a stale lock had referenced a still-running interpreter,
    the next worker would have caught it before this call).
    """
    path = _lock_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "version":      1,
                    "interpreter":  sys.executable,
                    "recorded_by":  recorded_by,
                    "recorded_pid": os.getpid(),
                    "recorded_at":  _now_iso(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:
        logger.exception(
            "[InterpreterCheck] Could not record interpreter to %s — "
            "single-interpreter invariant cannot be enforced this session.",
            path,
        )


def check_interpreter(data_dir: Path) -> InterpreterCheckResult:
    """Compare the current process's interpreter to the recorded one.

    Returns an :class:`InterpreterCheckResult` describing the match.
    A missing lock file yields a "match" — we cannot disagree with
    ourselves.  An unreadable lock file yields a "match" with a note,
    on the principle that fail-open is correct for an informational
    check.
    """
    path = _lock_path(data_dir)
    actual = sys.executable
    if not path.exists():
        return InterpreterCheckResult(
            matches=True,
            expected_interpreter=None,
            actual_interpreter=actual,
            recorded_by=None,
            note="no prior interpreter recorded — nothing to compare against",
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        expected = data.get("interpreter")
        by       = data.get("recorded_by")
    except Exception:
        logger.exception("[InterpreterCheck] Could not read %s", path)
        return InterpreterCheckResult(
            matches=True,
            expected_interpreter=None,
            actual_interpreter=actual,
            recorded_by=None,
            note=f"could not read {path} — falling back to fail-open",
        )

    if not expected:
        return InterpreterCheckResult(
            matches=True,
            expected_interpreter=None,
            actual_interpreter=actual,
            recorded_by=by,
            note="lock file missing 'interpreter' key — falling back to fail-open",
        )

    # Compare by canonical absolute path so case / trailing-slash differences
    # don't cause false alarms on Windows.
    expected_norm = os.path.normcase(os.path.realpath(expected))
    actual_norm   = os.path.normcase(os.path.realpath(actual))

    if expected_norm == actual_norm:
        return InterpreterCheckResult(
            matches=True,
            expected_interpreter=expected,
            actual_interpreter=actual,
            recorded_by=by,
            note="interpreter matches recorded value",
        )

    return InterpreterCheckResult(
        matches=False,
        expected_interpreter=expected,
        actual_interpreter=actual,
        recorded_by=by,
        note=(
            f"interpreter MISMATCH: this process uses '{actual}' but the "
            f"{by or 'previous'} process recorded '{expected}'. "
            "Tool pip-installs will not be visible across both interpreters. "
            "Restart all systemu processes from the same Python."
        ),
    )


def assert_or_warn(data_dir: Path, *, recorded_by: str) -> InterpreterCheckResult:
    """Convenience: check, log loudly on mismatch, never raise.

    Intended for use by worker startup code:

        from systemu.runtime.interpreter_check import assert_or_warn
        assert_or_warn(Path("data"), recorded_by="worker")

    On match: returns the result silently (caller can ignore).
    On mismatch: logs WARNING + emits an Event Log entry so the operator
    sees it on the dashboard.  Does NOT call ``sys.exit`` — operators
    running mismatched setups today would otherwise lose service.
    """
    result = check_interpreter(data_dir)
    if result.matches:
        return result

    logger.warning("[InterpreterCheck] %s", result.note)
    try:
        # Avoid hard import of event log at module-load time so the
        # interpreter check is usable in contexts where notifications
        # aren't initialised yet (early daemon boot).
        from systemu.interface.notifications import log_event
        log_event(
            "WARNING", "runtime",
            "Interpreter mismatch detected",
            {
                "recorded_by":          result.recorded_by,
                "expected_interpreter": result.expected_interpreter,
                "actual_interpreter":   result.actual_interpreter,
                "actor":                recorded_by,
                "hint": (
                    "Tool dependency installs may not propagate across "
                    "processes. Restart daemon and worker(s) from the same "
                    "Python interpreter."
                ),
            },
        )
    except Exception:
        # Event-log failure should never crash the boot.
        logger.debug("[InterpreterCheck] could not emit event-log entry", exc_info=True)
    return result


# opt-in strict mode.  Production operators who have shaken out
# their launcher set ``SYSTEMU_STRICT_INTERPRETER=1`` so a mismatched
# worker exits immediately instead of silently running with a different
# pip site-packages than the daemon.
def assert_or_fail(
    data_dir: Path,
    *,
    recorded_by: str,
    strict_env_var: str = "SYSTEMU_STRICT_INTERPRETER",
    exit_fn=None,
) -> InterpreterCheckResult:
    """Like :func:`assert_or_warn`, but hard-fail when strict mode is on.

    Hard-fail behaviour: print a remediation message to ``stderr`` and
    call ``exit_fn(1)`` (defaults to :func:`sys.exit`).  ``exit_fn`` is
    injectable for tests.

    When ``$SYSTEMU_STRICT_INTERPRETER`` is unset or "0" / "false", the
    function falls back to :func:`assert_or_warn` so production deploys
    can opt in without touching dev environments.
    """
    import os as _os
    import sys as _sys
    strict = (_os.environ.get(strict_env_var, "0").strip().lower()
              in ("1", "true", "yes"))
    if not strict:
        return assert_or_warn(data_dir, recorded_by=recorded_by)

    result = check_interpreter(data_dir)
    if result.matches:
        return result

    # Hard-fail path — emit the same Event Log entry first so the
    # mismatch is recorded even though the process is about to exit.
    try:
        from systemu.interface.notifications import log_event
        log_event(
            "ERROR", "runtime",
            "Interpreter mismatch — strict mode is on, exiting",
            {
                "recorded_by":          result.recorded_by,
                "expected_interpreter": result.expected_interpreter,
                "actual_interpreter":   result.actual_interpreter,
                "actor":                recorded_by,
            },
        )
    except Exception:
        pass

    msg = (
        f"\n[InterpreterCheck] FATAL — strict mode (SYSTEMU_STRICT_INTERPRETER=1)\n"
        f"  Expected interpreter (recorded by {result.recorded_by or 'previous process'}):\n"
        f"    {result.expected_interpreter}\n"
        f"  Actual interpreter ({recorded_by}):\n"
        f"    {result.actual_interpreter}\n"
        f"\n"
        f"Tool dependency installs will not be visible across both interpreters.\n"
        f"Remediation: restart all systemu processes from the same Python, e.g.\n"
        f"    `python -m sharing_on daemon stop && python -m sharing_on daemon start`\n"
        f"using the SAME `python` for daemon and worker(s).\n"
    )
    print(msg, file=_sys.stderr)
    logger.error("[InterpreterCheck] strict-mode hard-fail: %s", result.note)
    (exit_fn or _sys.exit)(1)
    return result
