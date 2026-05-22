#!/usr/bin/env python3
"""api_call_get — Perform a GET request to a REST API endpoint.

Parameters (via run() kwargs):
  url (str, required): The API endpoint URL
  headers (dict, optional): Optional headers for authentication or content type. Default {}.
  timeout (int, optional): Timeout in seconds. Default 30.

Returns (dict):
  success (bool): True if the operation succeeded.
  data (dict): The JSON response body.
  error (str|None): Error message on failure, None on success.
"""
from __future__ import annotations
import requests

TOOL_META = {
    "name": "api_call_get",
    "tool_type": "api_call",
    "dependencies": ["requests"],
}


def run(url: str, headers: dict = None, timeout: int = 30) -> dict:
    """Perform a GET request to a REST API endpoint and return JSON data."""
    if not url:
        return {"success": False, "data": {}, "error": "url is required"}

    if headers is None:
        headers = {}

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code == 404:
            return {
                "success": False,
                "data": {},
                "error": (
                    f"404 Not Found: {url}. This endpoint may require authentication or does not exist. "
                    "Try using web_extract_text(url) to scrape the public pricing/docs page instead."
                ),
            }
        response.raise_for_status()
        return {
            "success": True,
            "data": response.json(),
            "error": None
        }
    except requests.exceptions.RequestException as exc:
        return {
            "success": False,
            "data": {},
            "error": str(exc)
        }
    except ValueError as exc:
        return {
            "success": False,
            "data": {},
            "error": f"Failed to parse JSON response: {str(exc)}"
        }
    except Exception as exc:
        return {
            "success": False,
            "data": {},
            "error": f"An unexpected error occurred: {str(exc)}"
        }
