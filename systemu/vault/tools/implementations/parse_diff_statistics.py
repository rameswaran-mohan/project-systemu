#!/usr/env/bin python3
"""parse_diff_statistics — Parse a unified diff string (patch content) to count additions, deletions, total changes, and number of hunks.

Parameters (via run() kwargs):
  patch (str, required): Unified diff patch content from GitHub API.

Returns (dict):
  success (bool): True if parsing succeeded (even if patch is empty).
  additions (int): Number of added lines (lines starting with '+', excluding '+++').
  deletions (int): Number of deleted lines (lines starting with '-', excluding '---').
  total_changes (int): Total number of changed lines (additions + deletions).
  hunk_count (int): Number of hunks (sections starting with '@@').
  error (str|None): Error message or None.
"""
from __future__ import annotations

TOOL_META = {
    "name": "parse_diff_statistics",
    "tool_type": "python_function",
    "dependencies": [],
}


def run(patch: str = None) -> dict:
    """Parse a unified diff string and return line-change statistics.

    Returns:
        success (bool): True if the operation succeeded.
        additions (int): Number of added lines.
        deletions (int): Number of deleted lines.
        total_changes (int): Total changed lines.
        hunk_count (int): Number of hunks.
        error (str|None): Error message on failure, None on success.
    """
    # Handle None or empty patch gracefully
    if not patch:
        return {
            "success": True,
            "additions": 0,
            "deletions": 0,
            "total_changes": 0,
            "hunk_count": 0,
            "error": None,
        }

    try:
        lines = patch.splitlines()
        additions = 0
        deletions = 0
        hunk_count = 0

        for line in lines:
            # Count hunks: lines starting with '@@'
            if line.startswith('@@'):
                hunk_count += 1
            # Count additions: lines starting with '+' but not '+++'
            elif line.startswith('+') and not line.startswith('+++'):
                additions += 1
            # Count deletions: lines starting with '-' but not '---'
            elif line.startswith('-') and not line.startswith('---'):
                deletions += 1

        total_changes = additions + deletions

        return {
            "success": True,
            "additions": additions,
            "deletions": deletions,
            "total_changes": total_changes,
            "hunk_count": hunk_count,
            "error": None,
        }
    except Exception as exc:
        return {
            "success": False,
            "additions": 0,
            "deletions": 0,
            "total_changes": 0,
            "hunk_count": 0,
            "error": str(exc),
        }
