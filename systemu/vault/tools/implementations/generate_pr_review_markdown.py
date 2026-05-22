#!/usr/bin/env python3
"""generate_pr_review_markdown — Generate a structured Markdown review checklist from parsed PR file data.

Parameters (via run() kwargs):
  owner (str, required): GitHub repository owner.
  repo (str, required): GitHub repository name.
  pull_number (int, required): Pull request number.
  files (list[dict], required): List of file change dicts with fields:
      filename (str), status (str), additions (int), deletions (int),
      total_changes (int), hunk_count (int), language (str), high_churn (bool).

Returns (dict):
  success (bool): True if generation succeeded.
  markdown (str): Generated Markdown content.
  error (str|None): Error message or None.
"""
from __future__ import annotations

TOOL_META = {
    "name": "generate_pr_review_markdown",
    "tool_type": "python_function",
    "dependencies": [],
}


def run(owner: str, repo: str, pull_number: int, files: list) -> dict:
    """Generate a structured Markdown review checklist from parsed PR file data."""
    # Validate required parameters
    if not owner:
        return {"success": False, "markdown": "", "error": "owner is required"}
    if not repo:
        return {"success": False, "markdown": "", "error": "repo is required"}
    if not pull_number:
        return {"success": False, "markdown": "", "error": "pull_number is required"}
    if not files or not isinstance(files, list):
        return {"success": False, "markdown": "", "error": "files must be a non-empty list"}

    try:
        # Compute summary statistics
        total_files = len(files)
        total_additions = sum(f.get("additions", 0) for f in files)
        total_deletions = sum(f.get("deletions", 0) for f in files)
        high_churn_files = [f for f in files if f.get("high_churn", False)]
        high_churn_count = len(high_churn_files)

        # Build header
        lines = []
        lines.append(f"# PR Review: {owner}/{repo} #{pull_number}")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        lines.append(f"| Metric | Value |")
        lines.append(f"|--------|-------|")
        lines.append(f"| Total Files Changed | {total_files} |")
        lines.append(f"| Total Additions | {total_additions} |")
        lines.append(f"| Total Deletions | {total_deletions} |")
        lines.append(f"| High Churn Files | {high_churn_count} |")
        lines.append("")

        # Build files table
        lines.append("## Changed Files")
        lines.append("")
        lines.append("| File | Language | Status | +Added | -Deleted | Hunks | High Churn? |")
        lines.append("|------|----------|--------|--------|----------|-------|-------------|")
        for f in files:
            filename = f.get("filename", "")
            language = f.get("language", "")
            status = f.get("status", "")
            additions = f.get("additions", 0)
            deletions = f.get("deletions", 0)
            hunk_count = f.get("hunk_count", 0)
            high_churn = "Yes" if f.get("high_churn", False) else "No"
            lines.append(f"| {filename} | {language} | {status} | +{additions} | -{deletions} | {hunk_count} | {high_churn} |")
        lines.append("")

        # Build review checklist
        lines.append("## Review Checklist")
        lines.append("")
        lines.append("- [ ] **Security review**: No hardcoded secrets, SQL injection, XSS, or unsafe deserialization.")
        lines.append("- [ ] **Test coverage**: New/changed code has corresponding unit/integration tests.")
        lines.append("- [ ] **Documentation updates**: README, API docs, or inline comments updated if needed.")
        lines.append("- [ ] **Performance impact**: No obvious O(n^2) loops, unnecessary DB queries, or memory leaks.")
        lines.append("- [ ] **Error handling**: All error paths are handled gracefully; no silent failures.")
        lines.append("- [ ] **Logging**: Appropriate log levels used; no sensitive data logged.")
        lines.append("- [ ] **Backward compatibility**: Changes do not break existing API contracts or data formats.")
        lines.append("- [ ] **Dependency changes**: New dependencies are justified and have compatible licenses.")
        lines.append("")

        # High churn warning section
        if high_churn_count > 0:
            lines.append("## ⚠️ High Churn Warning")
            lines.append("")
            lines.append("The following files exceed the churn threshold and may require extra scrutiny:")
            lines.append("")
            for f in high_churn_files:
                filename = f.get("filename", "")
                total_changes = f.get("total_changes", 0)
                lines.append(f"- `{filename}` ({total_changes} total changes)")
            lines.append("")

        markdown = "\n".join(lines)

        return {"success": True, "markdown": markdown, "error": None}

    except Exception as exc:
        return {"success": False, "markdown": "", "error": str(exc)}
