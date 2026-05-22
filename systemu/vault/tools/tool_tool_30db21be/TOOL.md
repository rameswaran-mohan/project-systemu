---
name: web_search
tool_type: api_call
status: deployed
enabled: true
dependencies:
  - requests
---

# web_search

## Description

Search the web via DuckDuckGo and return results with title, URL, and snippet

## Parameters

- query (string, optional): Search query
- max_results (integer, default: 5): Maximum results to return

## Returns

- success (boolean)
- results (array)
- error (string)

## Implementation Notes

_No implementation notes yet._
