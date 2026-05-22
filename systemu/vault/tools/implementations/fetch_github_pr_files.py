#!/usr/bin/env python3
"""fetch_github_pr_files — Fetch all changed files from a GitHub pull request using the REST API, handling pagination.

Parameters (via run() kwargs):
  owner (str, required): GitHub repository owner (user or organization).
  repo (str, required): GitHub repository name.
  pull_number (int, required): Pull request number.
  per_page (int, optional): Number of files per page (max 100). Default 100.

Returns (dict):
  success (bool): True if the operation succeeded.
  files (list): List of file change objects from GitHub API (each contains filename, status, additions, deletions, changes, patch, etc.).
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations

import requests

TOOL_META = {
    "name": "fetch_github_pr_files",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(owner: str, repo: str, pull_number: int, per_page: int = 100) -> dict:
    """Fetch all changed files from a GitHub pull request, handling pagination."""
    if not owner:
        return {"success": False, "files": [], "error": "owner is required"}
    if not repo:
        return {"success": False, "files": [], "error": "repo is required"}
    if not pull_number or not isinstance(pull_number, int) or pull_number < 1:
        return {"success": False, "files": [], "error": "pull_number must be a positive integer"}
    if per_page < 1 or per_page > 100:
        return {"success": False, "files": [], "error": "per_page must be between 1 and 100"}

    try:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "python-requests",
        }
        all_files = []
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/files?per_page={per_page}"

        while url:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                return {"success": False, "files": [], "error": f"Unexpected response format: {data}"}
            all_files.extend(data)

            # Parse Link header for next page
            link_header = response.headers.get("Link", "")
            next_url = None
            if link_header:
                # Link header format: <https://api.github.com/...>; rel="next", <https://...>; rel="last"
                parts = link_header.split(",")
                for part in parts:
                    section = part.strip()
                    if 'rel="next"' in section:
                        # Extract URL from <...>
                        start = section.find("<")
                        end = section.find(">")
                        if start != -1 and end != -1:
                            next_url = section[start + 1:end]
                            break
            url = next_url

        return {"success": True, "files": all_files, "error": None}

    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        return {"success": False, "files": [], "error": f"HTTP {status_code}: {str(exc)}"}
    except requests.exceptions.Timeout:
        return {"success": False, "files": [], "error": "Request timed out after 30 seconds"}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "files": [], "error": f"Request failed: {str(exc)}"}
    except Exception as exc:
        return {"success": False, "files": [], "error": str(exc)}
