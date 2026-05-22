---
name: github_list_workflow_files
tool_type: api_call
status: proposed
enabled: false
dependencies:
  - requests
---

# github_list_workflow_files

## Description

List and optionally read workflow YAML files from the .github/workflows directory of a GitHub repository

## Parameters

- owner (string, optional): Repository owner
- repo (string, optional): Repository name
- branch (string, default: master): Branch to list files from
- read_contents (boolean, default: False): If true, fetch the raw content of each YAML file

## Returns

- success (boolean)
- files (array): List of file objects with name, path, and optionally content (if read_contents=true)
- error (string)

## Implementation Notes

Use requests.get with URL https://api.github.com/repos/{owner}/{repo}/contents/.github/workflows?ref={branch}. Set Accept header to application/vnd.github.v3+json. Parse JSON response. If read_contents is true, for each file with type 'file', fetch its download_url and store the raw text. Catch requests.RequestException and return error.
