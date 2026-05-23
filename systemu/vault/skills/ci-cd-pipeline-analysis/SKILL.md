---
name: ci-cd-pipeline-analysis
description: Proficiency in analyzing CI/CD pipeline runs on GitHub Actions, including
  filtering for failures, extracting run metadata, reviewing associated commits, and
  inspecting workflow definitions
metadata:
  category: devops
  proficiency_level: intermediate
  required_tools:
  - fetch_json
  - fetch_html
  - web_extract_text
  - file_list_dir
  - file_read
---

# ci_cd_pipeline_analysis

## Description

Proficiency in analyzing CI/CD pipeline runs on GitHub Actions, including filtering for failures, extracting run metadata, reviewing associated commits, and inspecting workflow definitions

## Procedural Instructions

To analyze failed CI/CD runs: 1) Use the GitHub API (fetch_json) to list workflow runs filtered by status=failure from the repository's actions endpoint. 2) For a specific run ID, fetch the run details and job logs via the API. 3) Retrieve the associated commit details using the commit hash endpoint. 4) List and read workflow YAML files from the .github/workflows directory using file_list_dir and file_read. 5) Compile all findings into a structured summary.

## Required Tools

- fetch_json
- fetch_html
- web_extract_text
- file_list_dir
- file_read

## Evidence Scrolls

- scroll_e9749c8e
