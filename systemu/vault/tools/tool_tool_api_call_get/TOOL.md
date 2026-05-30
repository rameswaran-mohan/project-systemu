---
name: api_call_get
tool_type: api_call
status: deployed
enabled: true
dependencies:
  - requests
---

# api_call_get

## Description

Perform a GET request to a REST API endpoint.

## Parameters

- url (string): The API endpoint URL
- headers (object, default: {}): Optional headers for authentication or content type
- timeout (integer, default: 30): Timeout in seconds

## Returns

- success (boolean)
- data (object) — The JSON response body
- error (string)

## Implementation Notes

_No implementation notes yet._
