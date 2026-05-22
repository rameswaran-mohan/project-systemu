---
name: github_get_commit
tool_type: api_call
status: proposed
enabled: false
dependencies:
  - requests
---

# github_get_commit

## Description

Get details about a specific commit in a GitHub repository

## Parameters

- owner (string, optional): Repository owner
- repo (string, optional): Repository name
- sha (string, optional): The commit SHA hash

## Returns

- success (boolean)
- commit (object): Commit object with sha, commit.message, commit.author, commit.committer, files (diff), stats, etc.
- error (string)

## Implementation Notes

Use requests.get with URL https://api.github.com/repos/{owner}/{repo}/commits/{sha}. Set Accept header to application/vnd.github.v3+json. Parse JSON response. Return the commit object. Catch requests.RequestException and return error.
