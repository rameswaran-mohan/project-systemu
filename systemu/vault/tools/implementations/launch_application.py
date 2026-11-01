#!/usr/bin/env python3
"""launch_application — Launch a desktop application.

Parameters (via run() kwargs):
  path             (str): Full path or executable name. (primary param)
  application_name (str): Alias for path — accepted for LLM compatibility.
  executable_name  (str): Alias for path — accepted for LLM compatibility.
  name             (str): Alias for path — accepted for LLM compatibility.

Any one of the above is sufficient. WHICH application runs is decided by
``systemu.runtime.app_targets.resolve_launch_target`` -- the same pure resolver
the approval gate scores, signs and shows (P26), called here on these exact
kwargs. This body acts on what it returns and on nothing else: it keeps no
friendly-name map and no alias order of its own. A call the resolver refuses
launches nothing.

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


def run(
    path: str = "",
    application_name: str = "",
    executable_name: str = "",
    name: str = "",
) -> dict:
    """Launch a desktop application non-blocking (Popen)."""
    from systemu.runtime.app_targets import AppTarget, resolve_launch_target

    target = resolve_launch_target({
        "path": path,
        "application_name": application_name,
        "executable_name": executable_name,
        "name": name,
    })
    if type(target) is not AppTarget:
        return {"success": False, "pid": None,
                "error": "Nothing was launched: " + target.reason + "."}
    resolved = target.value

    # Try Popen with shell=True — handles both executables and Windows URI schemes
    try:
        if sys.platform == "win32" and target.kind == "uri":
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
