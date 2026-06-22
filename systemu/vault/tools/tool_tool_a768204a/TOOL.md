---
name: compress_files
tool_type: file_operation
status: deployed
enabled: true
dependencies: []
---

# compress_files

## Description

Compress files into a ZIP archive

## Parameters

- output_path (string, optional): Path for the output zip file
- files (array, default: []): List of file paths to include
- include_dir (string, default: ): Add all files from this directory

## Returns

- success (boolean)
- output_path (string)
- file_count (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
