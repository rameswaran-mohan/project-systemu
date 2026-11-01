#!/usr/bin/env python3
"""close_application — Close a running application by name or PID.

Tries a graceful close (WM_CLOSE) first, then taskkill if still running.

WHICH process is stopped is decided by
``systemu.runtime.app_targets.resolve_close_target`` -- the same pure resolver
the approval gate scores, signs and shows (P26), called here on these exact
kwargs. This body acts on what it returns and on nothing else. A call the
resolver refuses stops nothing.

Parameters (via run() kwargs):
  application_name (str): Friendly name or executable name (e.g. "Snipping Tool",
                          "Microsoft Word", "SnippingTool.exe").
  name             (str): Alias for application_name.
  executable       (str): Alias for application_name.
  pid              (int, optional): Process ID to kill directly.
  force            (bool, optional): Skip graceful close, kill immediately (default False).

Returns (dict):
  success   (bool): True if the process was terminated.
  killed    (int):  Number of processes terminated.
  error     (str|None): Error message on failure, otherwise None.
"""
from __future__ import annotations

import subprocess
import sys

TOOL_META = {
    "name": "close_application",
    "tool_type": "desktop_action",
    "dependencies": [],
}


def run(
    application_name: str = "",
    name: str = "",
    executable: str = "",
    pid: int = None,
    force: bool = False,
) -> dict:
    """Close an application gracefully (or by force if requested)."""
    from systemu.runtime.app_targets import AppTarget, resolve_close_target

    target = resolve_close_target({
        "application_name": application_name,
        "name": name,
        "executable": executable,
        "pid": pid,
        "force": force,
    })
    if type(target) is not AppTarget:
        return {"success": False, "killed": 0,
                "error": "Nothing was closed: " + target.reason + "."}
    if target.kind == "pid":
        return _kill_pid(int(target.value), force=target.force)

    resolved = target.value
    if sys.platform == "win32":
        return _taskkill_windows(resolved, force=target.force)

    # Unix fallback via pkill
    try:
        result = subprocess.run(["pkill", "-f", resolved], capture_output=True)
        if result.returncode == 0:
            return {"success": True, "killed": 1, "error": None}
        return {"success": False, "killed": 0, "error": f"pkill returned {result.returncode}"}
    except Exception as exc:
        return {"success": False, "killed": 0, "error": str(exc)}


def _taskkill_windows(executable: str, *, force: bool) -> dict:
    flags = ["/F"] if force else []
    try:
        result = subprocess.run(
            ["taskkill"] + flags + ["/IM", executable],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return {"success": True, "killed": 1, "error": None}
        # If graceful failed, escalate to force
        if not force:
            result2 = subprocess.run(
                ["taskkill", "/F", "/IM", executable],
                capture_output=True, text=True,
            )
            if result2.returncode == 0:
                return {"success": True, "killed": 1, "error": None}
        return {"success": False, "killed": 0, "error": result.stdout.strip() or result.stderr.strip()}
    except Exception as exc:
        return {"success": False, "killed": 0, "error": str(exc)}


def _kill_pid(pid: int, *, force: bool) -> dict:
    try:
        import os, signal
        os.kill(pid, signal.SIGTERM if not force else signal.SIGKILL)
        return {"success": True, "killed": 1, "error": None}
    except Exception as exc:
        return {"success": False, "killed": 0, "error": str(exc)}
