#!/usr/bin/env python3
"""run_cli_command — Execute a shell command in the container and capture its stdout and stderr output.

Parameters (via run() kwargs):
  command (str, required): Shell command to execute (e.g., 'python --version').
  timeout_seconds (int, optional): Maximum time to wait for command completion. Default 30.

Returns (dict):
  success (bool): True if exit code was 0.
  stdout (str): Standard output.
  stderr (str): Standard error.
  return_code (int): Process exit code.
  error (str|None): Error message or None.
"""
from __future__ import annotations
import subprocess
import shlex

TOOL_META = {
    "name": "run_cli_command",
    "tool_type": "cli_command",
    "dependencies": [],
}


def run(command: str, timeout_seconds: int = 30) -> dict:
    """Execute command (as a list via shlex.split, never shell=True) and return output."""
    if not command:
        return {"success": False, "stdout": "", "stderr": "", "return_code": -1, "error": "command is required"}
    try:
        args = shlex.split(command)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,   # NEVER shell=True
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "return_code": result.returncode,
            "error": None if result.returncode == 0 else f"Command exited with code {result.returncode}",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "", "return_code": -1, "error": f"Command timed out after {timeout_seconds}s"}
    except Exception as exc:
        return {"success": False, "stdout": "", "stderr": "", "return_code": -1, "error": str(exc)}