---
name: file_list_dir
tool_type: file_operation
status: deployed
enabled: true
dependencies: []
---

# file_list_dir

## Description

List files in a directory with optional glob pattern filtering

## Parameters

- path (string, optional): Directory path
- pattern (string, default: *): Glob pattern
- recursive (boolean, default: False): Recurse into subdirectories

## Returns

- success (boolean)
- files (array)
- count (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
