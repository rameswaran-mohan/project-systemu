---
name: fetch_json
tool_type: api_call
status: deployed
enabled: true
dependencies:
  - requests
---

# fetch_json

## Description

HTTP GET a JSON API endpoint and return the parsed response data

## Parameters

- url (string, optional): Endpoint URL
- headers (object, default: {}): Optional request headers
- params (object, default: {}): Optional query parameters

## Returns

- success (boolean)
- data (any)
- status_code (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
