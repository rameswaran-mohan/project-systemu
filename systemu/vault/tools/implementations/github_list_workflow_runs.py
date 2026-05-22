#!/usr/bin/env python3
"""github_list_workflow_runs — List workflow runs for a GitHub repository, with optional filtering by status and branch.

Parameters (via run() kwargs):
  owner (str, required): Repository owner (user or organization).
  repo (str, required): Repository name.
  status (str, optional): Filter by status: completed, failure, success, cancelled, etc. Default "failure".
  branch (str, optional): Filter by branch name. Default "".
  per_page (int, optional): Number of results per page (max 100). Default 30.

Returns (dict):
  success (bool): True if the API call succeeded.
  runs (list): List of workflow run objects with id, name, status, conclusion, created_at, head_sha, etc.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations

TOOL_META = {
    "name": "github_list_workflow_runs",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(owner: str, repo: str, status: str = "failure", branch: str = "", per_page: int = 30) -> dict:
    """List workflow runs for a GitHub repository, with optional filtering by status and branch."""
    if not owner:
        return {"success": False, "runs": [], "error": "owner is required"}
    if not repo:
        return {"success": False, "runs": [], "error": "repo is required"}

    try:
        import requests

        url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
        headers = {
            "Accept": "application/vnd.github.v3+json",
        }
        params = {
            "per_page": min(per_page, 100),
        }
        if status:
            params["status"] = status
        if branch:
            params["branch"] = branch

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        runs = data.get("workflow_runs", [])
        return {
            "success": True,
            "runs": runs,
            "error": None,
        }

    except requests.exceptions.RequestException as exc:
        return {"success": False, "runs": [], "error": str(exc)}
    except Exception as exc:
        return {"success": False, "runs": [], "error": str(exc)}