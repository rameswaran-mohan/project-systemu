#!/usr/bin/env python3
"""github_get_workflow_run — Get detailed information about a specific workflow run, including jobs and logs

Parameters (via run() kwargs):
  owner (str, required): Repository owner
  repo (str, required): Repository name
  run_id (int, required): The ID of the workflow run

Returns (dict):
  success (bool): True if the operation succeeded.
  run (dict|None): Full workflow run object with id, name, status, conclusion, head_commit, jobs_url, logs_url, etc.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations

import requests

TOOL_META = {
    "name": "github_get_workflow_run",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(owner: str, repo: str, run_id: int) -> dict:
    """Get detailed information about a specific workflow run."""
    if not owner or not repo or not run_id:
        return {"success": False, "run": None, "error": "owner, repo, and run_id are required"}
    try:
        url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}"
        headers = {"Accept": "application/vnd.github.v3+json"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        run_data = response.json()
        return {"success": True, "run": run_data, "error": None}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "run": None, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "run": None, "error": str(exc)}