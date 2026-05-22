---
name: github_list_workflow_runs
tool_type: api_call
status: proposed
enabled: false
dependencies:
  - requests
---

# github_list_workflow_runs

## Description

List workflow runs for a GitHub repository, with optional filtering by status (e.g., failure) and branch

## Parameters

- owner (string, optional): Repository owner (user or organization)
- repo (string, optional): Repository name
- status (string, default: failure): Filter by status: completed, failure, success, cancelled, etc.
- branch (string, default: ): Filter by branch name (optional)
- per_page (integer, default: 30): Number of results per page (max 100)

## Returns

- success (boolean)
- runs (array): List of workflow run objects with id, name, status, conclusion, created_at, head_sha, etc.
- error (string)

## Implementation Notes

Use requests.get with URL https://api.github.com/repos/{owner}/{repo}/actions/runs. Pass params: status, branch, per_page. Set Accept header to application/vnd.github.v3+json. Parse JSON response. Return the 'workflow_runs' array. Catch requests.RequestException and return error.
