#!/usr/bin/env python3
"""github_get_commit — Get details about a specific commit in a GitHub repository.

Parameters (via run() kwargs):
  owner (str, required): Repository owner.
  repo (str, required): Repository name.
  sha (str, required): The commit SHA hash.

Returns (dict):
  success (bool): True if the commit was retrieved successfully.
  commit (dict|None): Commit object with sha, commit.message, commit.author, commit.committer, files (diff), stats, etc.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations

TOOL_META = {
    "name": "github_get_commit",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(owner: str, repo: str, sha: str) -> dict:
    """Get details about a specific commit in a GitHub repository.

    Returns:
        success (bool): True if the commit was retrieved successfully.
        commit (dict|None): Commit object with sha, commit.message, commit.author, commit.committer, files (diff), stats, etc.
        error (str|None): Error message on failure, None on success.
    """
    if not owner:
        return {"success": False, "commit": None, "error": "owner is required"}
    if not repo:
        return {"success": False, "commit": None, "error": "repo is required"}
    if not sha:
        return {"success": False, "commit": None, "error": "sha is required"}

    try:
        import requests
        url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}"
        headers = {"Accept": "application/vnd.github.v3+json"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        commit_data = response.json()
        return {"success": True, "commit": commit_data, "error": None}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "commit": None, "error": str(exc)}
    except Exception as exc:
        return {"success": False, "commit": None, "error": str(exc)}