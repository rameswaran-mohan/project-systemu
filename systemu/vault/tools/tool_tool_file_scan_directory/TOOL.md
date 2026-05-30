---
name: file_scan_directory
tool_type: python_function
status: deployed
enabled: true
dependencies:
  []
---

# file_scan_directory

## Description

Recursively scan a directory and collect file metadata.

## Parameters

- source_path (string): Absolute or relative path to the directory to scan

## Returns

- success (boolean)
- files (array) — List of {name, extension, size_bytes, modified_at, relative_path}
- error (string)

## Implementation Notes

_No implementation notes yet._
