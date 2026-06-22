#!/usr/bin/env python3
"""launch_application — Launch a desktop application.

Parameters (via run() kwargs):
  path             (str): Full path or executable name. (primary param)
  application_name (str): Alias for path — accepted for LLM compatibility.
  executable_name  (str): Alias for path — accepted for LLM compatibility.
  name             (str): Alias for path — accepted for LLM compatibility.

Any one of the above is sufficient. They are checked in order.

Returns (dict):
  success (bool): True if the process started successfully.
  pid     (int|None): Process ID of the launched application, or None.
  error   (str|None): Error message on failure, otherwise None.
"""
from __future__ import annotations

import subprocess
import sys
import time

TOOL_META = {
    "name": "launch_application",
    "tool_type": "cli_command",
    "dependencies": [],
}

# Common Windows application name → executable mapping
_APP_MAP = {
    "snipping tool":    "SnippingTool.exe",
    "snippingtool":     "SnippingTool.exe",
    "snip & sketch":    "ms-screenclip:",
    "microsoft word":   "WINWORD.EXE",
    "word":             "WINWORD.EXE",
    "microsoft excel":  "EXCEL.EXE",
    "excel":            "EXCEL.EXE",
    "microsoft powerpoint": "POWERPNT.EXE",
    "powerpoint":       "POWERPNT.EXE",
    "notepad":          "notepad.exe",
    "notepad++":        "notepad++.exe",
    "google chrome":    "chrome.exe",
    "chrome":           "chrome.exe",
    "microsoft edge":   "msedge.exe",
    "edge":             "msedge.exe",
    "firefox":          "firefox.exe",
    "explorer":         "explorer.exe",
    "file explorer":    "explorer.exe",
    "cmd":              "cmd.exe",
    "powershell":       "powershell.exe",
    "paint":            "mspaint.exe",
    "calculator":       "calc.exe",
    "task manager":     "taskmgr.exe",
}


def run(
    path: str = "",
    application_name: str = "",
    executable_name: str = "",
    name: str = "",
) -> dict:
    """Launch a desktop application non-blocking (Popen)."""
    target = path or application_name or executable_name or name
    if not target:
        return {
            "success": False,
            "pid": None,
            "error": "Provide path, application_name, executable_name, or name.",
        }

    # Resolve common friendly names to executable names
    resolved = _APP_MAP.get(target.strip().lower(), target)

    # Try Popen with shell=True — handles both executables and Windows URI schemes
    try:
        if sys.platform == "win32" and resolved.endswith(":"):
            # Windows URI scheme (e.g. ms-screenclip:)
            import os
            os.startfile(resolved)
            time.sleep(0.5)
            return {"success": True, "pid": None, "error": None}

        proc = subprocess.Popen(
            resolved,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.8)   # brief pause to let the window appear
        return {"success": True, "pid": proc.pid, "error": None}

    except Exception as popen_exc:
        # Windows fallback: use shell file-association open
        if sys.platform == "win32":
            try:
                import os
                os.startfile(resolved)
                time.sleep(0.8)
                return {"success": True, "pid": None, "error": None}
            except Exception as sf_exc:
                return {"success": False, "pid": None, "error": str(sf_exc)}
        return {"success": False, "pid": None, "error": str(popen_exc)}
