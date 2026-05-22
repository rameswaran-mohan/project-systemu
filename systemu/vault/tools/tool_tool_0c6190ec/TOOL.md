---
name: github_get_workflow_run
tool_type: api_call
status: proposed
enabled: false
dependencies:
  - requests
---

# github_get_workflow_run

## Description

Get detailed information about a specific workflow run, including jobs and logs

## Parameters

- owner (string, optional): Repository owner
- repo (string, optional): Repository name
- run_id (integer, optional): The ID of the workflow run

## Returns

- success (boolean)
- run (object): Full workflow run object with id, name, status, conclusion, head_commit, jobs_url, logs_url, etc.
- error (string)

## Implementation Notes

Use requests.get with URL https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}. Set Accept header to application/vnd.github.v3+json. Parse JSON response. Return the run object. Catch requests.RequestException and return error.
