---
name: fetch_html
tool_type: api_call
status: deployed
enabled: true
dependencies:
  - requests
---

# fetch_html

## Description

HTTP GET a URL and return the raw HTML response

## Parameters

- url (string, optional): URL to fetch
- headers (object, default: {}): Optional request headers

## Returns

- success (boolean)
- html (string)
- status_code (integer)
- error (string)

## Implementation Notes

_No implementation notes yet._
