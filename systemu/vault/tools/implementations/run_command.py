#!/usr/bin/env python3
"""Run a shell command and return stdout, stderr, and return code."""
from __future__ import annotations

import subprocess
from pathlib import Path

TOOL_META = {
    "name": "run_command",
    "tool_type": "system",
    "dependencies": [],
}


def run(**kwargs) -> dict:
    command: str = kwargs.get("command", "")
    timeout: int = int(kwargs.get("timeout", 30))
    cwd: str = kwargs.get("cwd", "")

    if not command:
        return {"success": False, "stdout": "", "stderr": "", "return_code": -1, "error": "command is required"}

    try:
        cwd_path = None
        if cwd:
            cwd_path = str(Path(cwd).expanduser())

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd_path,
        )

        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
            "error": None,
        }

    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "stdout": "",
            "stderr": "",
            "return_code": -1,
            "error": f"Command timed out after {timeout}s: {exc}",
        }
    except Exception as exc:
        return {"success": False, "stdout": "", "stderr": "", "return_code": -1, "error": str(exc)}
