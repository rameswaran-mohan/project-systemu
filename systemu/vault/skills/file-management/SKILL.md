---
name: file-management
description: Proficiency in reading, writing, copying, and organizing files on the
  local filesystem
metadata:
  category: file_ops
  proficiency_level: beginner
  required_tools:
  - file_read
  - file_write
  - file_copy
  - file_delete
  - file_list_dir
  - file_append
---

# file_management

## Description

Proficiency in reading, writing, copying, and organizing files on the local filesystem

## Procedural Instructions

For file operations: 1) Always expand ~ in paths — tools handle this automatically. 2) Use file_read before overwriting to verify you have the right content. 3) Use file_list_dir with a glob pattern to discover files (e.g. "*.docx", "**/*.csv"). 4) Prefer file_append for log-style writes to avoid overwriting existing content. 5) Before file_delete, confirm the path is the correct target — deletions are irreversible.

## Required Tools

- file_read
- file_write
- file_copy
- file_delete
- file_list_dir
- file_append

## Evidence Scrolls

_No evidence scrolls._
