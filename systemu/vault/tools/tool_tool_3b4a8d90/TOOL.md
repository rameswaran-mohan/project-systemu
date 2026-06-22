---
name: file_write
tool_type: file_operation
status: deployed
enabled: true
dependencies: []
---

# file_write

## Description

Write text content to a file, creating parent directories as needed

## Parameters

- path (string, optional): File path to write
- content (string, optional): Text content to write
- encoding (string, default: utf-8): Text encoding
- overwrite (boolean, default: True): Overwrite if file exists

## Returns

- success (boolean)
- path (string)
- size_bytes (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
