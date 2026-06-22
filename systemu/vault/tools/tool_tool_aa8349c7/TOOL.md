---
name: download_file
tool_type: api_call
status: deployed
enabled: true
dependencies:
  - requests
---

# download_file

## Description

Download a file from a URL to a local path

## Parameters

- url (string, optional): File URL to download
- output_path (string, optional): Local path to save the file
- overwrite (boolean, default: False): Overwrite if file exists

## Returns

- success (boolean)
- output_path (string)
- size_bytes (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
