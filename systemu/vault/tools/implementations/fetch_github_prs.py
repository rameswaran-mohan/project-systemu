#!/usr/bin/env python3
"""fetch_github_prs — Fetch open pull requests from a GitHub repository using the REST API.

Parameters (via run() kwargs):
  repo_owner (str, required): The owner of the repository.
  repo_name (str, required): The name of the repository.

Returns (dict):
  success (bool): True if the operation succeeded.
  data (list): List of open pull request objects.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
import requests

TOOL_META = {
    "name": "fetch_github_prs",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(repo_owner: str, repo_name: str) -> dict:
    """Fetch open pull requests from a GitHub repository."""
    if not repo_owner or not repo_name:
        return {"success": False, "data": [], "error": "repo_owner and repo_name are required"}

    url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/pulls"
    headers = {"Accept": "application/vnd.github.v3+json"}
    all_prs = []
    next_url = url

    try:
        while next_url:
            response = requests.get(next_url, headers=headers, timeout=30)
            response.raise_for_status()
            all_prs.extend(response.json())

            next_url = None
            if "Link" in response.headers:
                links = response.headers["Link"].split(",")
                for link in links:
                    if 'rel="next"' in link:
                        next_url = link.split(";")[0].strip(" <> ")
                        break

        return {"success": True, "data": all_prs, "error": None}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "data": [], "error": str(exc)}
    except Exception as exc:
        return {"success": False, "data": [], "error": f"Unexpected error: {str(exc)}"}
