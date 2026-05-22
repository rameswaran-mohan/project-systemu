#!/usr/bin/env python3
"""fetch_github_org_data — Retrieve public organization metadata and repository activity from the GitHub REST API.

Parameters (via run() kwargs):
  org_name (str, required): The GitHub organization handle.

Returns (dict):
  success (bool): True if the operation succeeded.
  data (dict): The combined organization and repository metadata.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
import requests

TOOL_META = {
    "name": "fetch_github_org_data",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(org_name: str) -> dict:
    """Retrieve public organization metadata and repository activity from the GitHub REST API."""
    if not org_name:
        return {"success": False, "data": {}, "error": "org_name is required"}

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "SystemU-Agent"
    }
    base_url = "https://api.github.com"

    try:
        org_resp = requests.get(f"{base_url}/orgs/{org_name}", headers=headers, timeout=10)
        if org_resp.status_code == 404:
            return {"success": False, "data": {}, "error": f"Organization '{org_name}' not found."}
        if org_resp.status_code == 403:
            return {"success": False, "data": {}, "error": "Rate limit exceeded or access forbidden."}
        org_resp.raise_for_status()
        org_data = org_resp.json()

        repos_resp = requests.get(f"{base_url}/orgs/{org_name}/repos", headers=headers, timeout=10)
        repos_resp.raise_for_status()
        repos_data = repos_resp.json()

        return {
            "success": True,
            "data": {
                "org_info": org_data,
                "repositories": repos_data
            },
            "error": None
        }

    except requests.exceptions.RequestException as exc:
        return {"success": False, "data": {}, "error": f"API request failed: {str(exc)}"}
    except Exception as exc:
        return {"success": False, "data": {}, "error": str(exc)}
